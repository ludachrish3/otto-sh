"""stage_use_case: rendered texts, otto.env, env_file: sidecars (spec §8)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.docker.resolve import UseCaseResolutionError
from otto.docker.staging import ComposeFileToStage, stage_use_case
from otto.result import CommandResult, Result
from otto.utils import Status


def _ok(out: str = "") -> CommandResult:
    return CommandResult(Status.Success, value=out, command="", retcode=0)


def _fail(out: str = "denied") -> CommandResult:
    return CommandResult(Status.Failed, value=out, command="", retcode=1)


def _parent(tmp_path):
    parent = MagicMock()
    parent.id = "test3"
    parent.exec = AsyncMock(return_value=_ok())
    staged: list[tuple[str, str, str]] = []  # (local basename, remote dir, staged content)

    async def _put(paths, dest):
        staged.extend((Path(p).name, str(dest), Path(p).read_text()) for p in paths)
        return Result(Status.Success)

    parent.put = AsyncMock(side_effect=_put)
    parent._staged = staged
    return parent


# ---------------------------------------------------------------------------
# Happy path: order, layout, content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_order_env_and_remote_paths(tmp_path):
    parent = _parent(tmp_path)
    files = [
        ComposeFileToStage("core", "services: {api: {image: x}}\n", tmp_path),
        ComposeFileToStage("edge", "services: {edge: {image: y}}\n", tmp_path),
    ]
    out = await stage_use_case(parent, "otto-integration-u", files, "A=1\n")
    assert [p.name for p in out.compose_paths] == ["core.yml", "edge.yml"]
    assert str(out.compose_paths[0].parent).endswith("/compose/0")
    assert str(out.compose_paths[1].parent).endswith("/compose/1")
    assert out.env_file.name == "otto.env"
    assert ("otto.env", str(out.env_file.parent), "A=1\n") in parent._staged
    # Content reached the recorded transport paired with the right handle —
    # not just "a put happened" (review I5).
    assert ("core.yml", str(out.compose_paths[0].parent), files[0].text) in parent._staged
    assert ("edge.yml", str(out.compose_paths[1].parent), files[1].text) in parent._staged


@pytest.mark.asyncio
async def test_the_env_file_uploaded_is_the_one_the_return_value_names(tmp_path):
    """DRIFT PIN: ONE helper names the preview, the return value AND the upload.

    Every value here is read OFF the returned path — nothing re-types
    ``otto.env`` — which is what makes this fail when the two ends disagree.
    The `-f` set got exactly this treatment (``sub = compose_paths[idx].parent``);
    the env file was staged from a separate literal, so renaming it in
    :func:`use_case_env_file` moved the previewed and RETURNED path while the
    upload stayed put: a live ``docker compose --env-file <path>`` pointing at a
    file nothing staged. The test above pins the NAME; this one pins that all
    three ends move together whatever the name is.
    """
    parent = _parent(tmp_path)
    files = [ComposeFileToStage("core", "services: {api: {image: x}}\n", tmp_path)]

    out = await stage_use_case(parent, "otto-integration-u", files, "A=1\n")

    assert (out.env_file.name, str(out.env_file.parent), "A=1\n") in parent._staged, (
        f"the env file the return value names was never uploaded: {parent._staged}"
    )


@pytest.mark.asyncio
async def test_env_file_sidecars_are_shipped_at_the_relative_dest_with_content(tmp_path):
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / "svc.env").write_text("K=1\n")
    text = "services:\n  api:\n    image: x\n    env_file: conf/svc.env\n"
    parent = _parent(tmp_path)
    out = await stage_use_case(parent, "p", [ComposeFileToStage("core", text, tmp_path)], "")
    expected_dir = str(out.compose_paths[0].parent / "conf")
    assert ("svc.env", expected_dir, "K=1\n") in parent._staged


@pytest.mark.asyncio
async def test_env_file_list_form_sidecar_is_shipped(tmp_path):
    (tmp_path / "a.env").write_text("A=1\n")
    (tmp_path / "b.env").write_text("B=1\n")
    text = "services:\n  api:\n    image: x\n    env_file: [a.env, b.env]\n"
    parent = _parent(tmp_path)
    out = await stage_use_case(parent, "p", [ComposeFileToStage("core", text, tmp_path)], "")
    expected_dir = str(out.compose_paths[0].parent)
    assert ("a.env", expected_dir, "A=1\n") in parent._staged
    assert ("b.env", expected_dir, "B=1\n") in parent._staged


@pytest.mark.asyncio
async def test_env_file_long_form_mapping_entry_is_shipped(tmp_path):
    """Compose Spec >= 2.24: `env_file: [{path: ..., required: false}]` (I3)."""
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / "svc.env").write_text("K=1\n")
    text = (
        "services:\n  api:\n    image: x\n"
        "    env_file:\n      - path: conf/svc.env\n        required: false\n"
    )
    parent = _parent(tmp_path)
    out = await stage_use_case(parent, "p", [ComposeFileToStage("core", text, tmp_path)], "")
    expected_dir = str(out.compose_paths[0].parent / "conf")
    assert ("svc.env", expected_dir, "K=1\n") in parent._staged


@pytest.mark.asyncio
async def test_a_sidecar_referenced_twice_in_one_file_is_staged_once(tmp_path):
    """M2: two services referencing the same env_file dedupe to one transfer."""
    (tmp_path / "shared.env").write_text("S=1\n")
    text = (
        "services:\n"
        "  api:\n    image: x\n    env_file: shared.env\n"
        "  worker:\n    image: y\n    env_file: shared.env\n"
    )
    parent = _parent(tmp_path)
    await stage_use_case(parent, "p", [ComposeFileToStage("core", text, tmp_path)], "")
    matches = [row for row in parent._staged if row[0] == "shared.env"]
    assert len(matches) == 1, matches
    # core.yml + the deduped shared.env + otto.env — never two shared.env round trips.
    assert parent.put.call_count == 3


@pytest.mark.asyncio
async def test_a_sidecar_only_lands_in_its_owning_numbered_dir(tmp_path):
    """M1 line 309 dead arm: the multi-file `continue` when a sidecar belongs elsewhere."""
    (tmp_path / "only-edge.env").write_text("E=1\n")
    files = [
        ComposeFileToStage("core", "services: {api: {image: x}}\n", tmp_path),
        ComposeFileToStage(
            "edge",
            "services:\n  edge:\n    image: y\n    env_file: only-edge.env\n",
            tmp_path,
        ),
    ]
    parent = _parent(tmp_path)
    out = await stage_use_case(parent, "p", files, "")
    core_dir = str(out.compose_paths[0].parent)
    edge_dir = str(out.compose_paths[1].parent)
    assert ("only-edge.env", edge_dir, "E=1\n") in parent._staged
    assert not any(row[0] == "only-edge.env" and row[1] == core_dir for row in parent._staged)


@pytest.mark.asyncio
async def test_a_non_dict_service_entry_is_skipped(tmp_path):
    """M1 line 244: a malformed (non-mapping) service value is skipped, not crashed on."""
    text = "services:\n  api: not-a-mapping\n"
    parent = _parent(tmp_path)
    out = await stage_use_case(parent, "p", [ComposeFileToStage("core", text, tmp_path)], "")
    assert [p.name for p in out.compose_paths] == ["core.yml"]


# ---------------------------------------------------------------------------
# Refusals: missing sidecars, escapes, unrecognized shapes (I2, I3, I4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_sidecar_is_refused(tmp_path):
    text = "services:\n  api:\n    image: x\n    env_file: [gone.env]\n"
    with pytest.raises(UseCaseResolutionError, match="references env_file"):
        await stage_use_case(
            _parent(tmp_path), "p", [ComposeFileToStage("core", text, tmp_path)], ""
        )


@pytest.mark.asyncio
async def test_missing_sidecar_names_handle_and_path(tmp_path):
    text = "services:\n  api:\n    image: x\n    env_file: [gone.env]\n"
    with pytest.raises(UseCaseResolutionError) as exc_info:
        await stage_use_case(
            _parent(tmp_path), "p", [ComposeFileToStage("core", text, tmp_path)], ""
        )
    message = str(exc_info.value)
    assert "'core'" in message
    assert "gone.env" in message


@pytest.mark.asyncio
async def test_unparseable_rendered_yaml_names_the_handle(tmp_path):
    with pytest.raises(UseCaseResolutionError, match="rendered text is not valid YAML"):
        await stage_use_case(
            _parent(tmp_path), "p", [ComposeFileToStage("core", ":\nnot yaml: [", tmp_path)], ""
        )


@pytest.mark.asyncio
async def test_non_mapping_rendered_yaml_is_refused_not_crashed(tmp_path):
    """I4: valid YAML whose top level isn't a mapping is a named refusal, not AttributeError."""
    with pytest.raises(UseCaseResolutionError, match="not a mapping"):
        await stage_use_case(
            _parent(tmp_path), "p", [ComposeFileToStage("core", "- a\n- b\n", tmp_path)], ""
        )


@pytest.mark.asyncio
async def test_scalar_rendered_yaml_is_refused_not_crashed(tmp_path):
    with pytest.raises(UseCaseResolutionError, match="not a mapping"):
        await stage_use_case(
            _parent(tmp_path), "p", [ComposeFileToStage("core", "just a string\n", tmp_path)], ""
        )


@pytest.mark.asyncio
async def test_unrecognized_env_file_list_entry_is_refused(tmp_path):
    """I3: an entry that is neither a string nor a `{path: ...}` mapping is refused loudly."""
    text = "services:\n  api:\n    image: x\n    env_file: [123]\n"
    with pytest.raises(UseCaseResolutionError, match="unrecognized env_file entry"):
        await stage_use_case(
            _parent(tmp_path), "p", [ComposeFileToStage("core", text, tmp_path)], ""
        )


@pytest.mark.asyncio
async def test_long_form_entry_missing_path_key_is_refused(tmp_path):
    text = "services:\n  api:\n    image: x\n    env_file:\n      - required: false\n"
    with pytest.raises(UseCaseResolutionError, match="unrecognized env_file entry"):
        await stage_use_case(
            _parent(tmp_path), "p", [ComposeFileToStage("core", text, tmp_path)], ""
        )


@pytest.mark.asyncio
async def test_unrecognized_env_file_value_shape_is_refused(tmp_path):
    text = "services:\n  api:\n    image: x\n    env_file: 123\n"
    with pytest.raises(UseCaseResolutionError, match="unrecognized env_file value"):
        await stage_use_case(
            _parent(tmp_path), "p", [ComposeFileToStage("core", text, tmp_path)], ""
        )


@pytest.mark.asyncio
async def test_bare_top_level_mapping_refusal_does_not_advertise_itself(tmp_path):
    """The refusal must not offer a shape it then rejects (Task 10 re-review N1).

    Compose's long form (``{path: ..., required: ...}``) is a LIST ENTRY, so a
    bare mapping AS the ``env_file:`` value is correctly refused — but the old
    message ended "a string, a list, or a mapping with a string 'path' key",
    which reads as if the very value just rejected were accepted. Anchored on
    the corrected clause so re-advertising it goes red.
    """
    text = "services:\n  api:\n    image: x\n    env_file:\n      path: a.env\n"
    with pytest.raises(UseCaseResolutionError) as excinfo:
        await stage_use_case(
            _parent(tmp_path), "p", [ComposeFileToStage("core", text, tmp_path)], ""
        )
    message = str(excinfo.value)
    assert "unrecognized env_file value" in message
    assert "A bare mapping is not a valid env_file value" in message
    assert "a string, or a list of strings and/or mappings" in message


@pytest.mark.asyncio
async def test_absolute_env_file_ref_is_refused(tmp_path):
    """I2: an absolute env_file ref must not stage outside /tmp/otto-docker."""
    text = "services:\n  api:\n    image: x\n    env_file: /etc/secrets.env\n"
    with pytest.raises(UseCaseResolutionError, match="escapes its staging root"):
        await stage_use_case(
            _parent(tmp_path), "p", [ComposeFileToStage("core", text, tmp_path)], ""
        )


@pytest.mark.asyncio
async def test_dotdot_env_file_ref_is_refused(tmp_path):
    """I2: a `../` ref that climbs above its numbered dir is refused the same way."""
    text = "services:\n  api:\n    image: x\n    env_file: ../../etc/passwd\n"
    with pytest.raises(UseCaseResolutionError, match="escapes its staging root"):
        await stage_use_case(
            _parent(tmp_path), "p", [ComposeFileToStage("core", text, tmp_path)], ""
        )


@pytest.mark.asyncio
async def test_dry_run_declines(tmp_path, monkeypatch):
    from otto.docker import staging as staging_mod
    from otto.result import CommandNotRunError

    monkeypatch.setattr(staging_mod, "is_dry_run", lambda: True)
    with pytest.raises(CommandNotRunError):
        await stage_use_case(_parent(tmp_path), "p", [], "")


# ---------------------------------------------------------------------------
# extra_files: adapter-generated env_file sidecars (I1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_generated_sidecar_referenced_by_rendered_text_is_staged(tmp_path):
    """A ref not found under source_dir is claimed by extra_files, not refused."""
    text = "services:\n  api:\n    image: x\n    env_file: conf/gen.env\n"
    parent = _parent(tmp_path)
    out = await stage_use_case(
        parent,
        "p",
        [ComposeFileToStage("core", text, tmp_path)],
        "",
        extra_files={"conf/gen.env": "G=1\n"},
    )
    expected_dir = str(out.compose_paths[0].parent / "conf")
    assert ("gen.env", expected_dir, "G=1\n") in parent._staged


@pytest.mark.asyncio
async def test_an_adapter_sidecar_only_lands_in_its_owning_numbered_dir(tmp_path):
    """The adapter-sidecar analog of the local-sidecar multi-file `continue` (M1 line 440)."""
    files = [
        ComposeFileToStage("core", "services: {api: {image: x}}\n", tmp_path),
        ComposeFileToStage(
            "edge",
            "services:\n  edge:\n    image: y\n    env_file: gen.env\n",
            tmp_path,
        ),
    ]
    parent = _parent(tmp_path)
    out = await stage_use_case(parent, "p", files, "", extra_files={"gen.env": "G=1\n"})
    core_dir = str(out.compose_paths[0].parent)
    edge_dir = str(out.compose_paths[1].parent)
    assert ("gen.env", edge_dir, "G=1\n") in parent._staged
    assert not any(row[0] == "gen.env" and row[1] == core_dir for row in parent._staged)


@pytest.mark.asyncio
async def test_unreferenced_extra_file_ships_once_under_compose_preserving_dir(tmp_path):
    text = "services: {api: {image: x}}\n"
    parent = _parent(tmp_path)
    out = await stage_use_case(
        parent,
        "p",
        [ComposeFileToStage("core", text, tmp_path)],
        "",
        extra_files={"conf/extra.env": "E=1\n"},
    )
    compose_root = str(out.compose_paths[0].parent.parent)  # compose/<n>'s parent = compose/
    expected_dir = f"{compose_root}/conf"
    assert ("extra.env", expected_dir, "E=1\n") in parent._staged


@pytest.mark.asyncio
async def test_unreferenced_flat_extra_file_ships_under_compose_root(tmp_path):
    text = "services: {api: {image: x}}\n"
    parent = _parent(tmp_path)
    out = await stage_use_case(
        parent,
        "p",
        [ComposeFileToStage("core", text, tmp_path)],
        "",
        extra_files={"flat.env": "F=1\n"},
    )
    compose_root = str(out.compose_paths[0].parent.parent)
    assert ("flat.env", compose_root, "F=1\n") in parent._staged


@pytest.mark.asyncio
async def test_extra_files_key_that_escapes_is_refused(tmp_path):
    """I2's guard also covers extra_files keys, not just repo env_file refs."""
    text = "services: {api: {image: x}}\n"
    with pytest.raises(UseCaseResolutionError, match="escapes its staging root"):
        await stage_use_case(
            _parent(tmp_path),
            "p",
            [ComposeFileToStage("core", text, tmp_path)],
            "",
            extra_files={"../evil.env": "x"},
        )


# ---------------------------------------------------------------------------
# M1: previously-dead failure arms
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_prepare_is_a_command_failure(tmp_path):
    from otto.host.errors import HostCommandError

    parent = _parent(tmp_path)
    parent.exec = AsyncMock(return_value=_fail("mkdir: permission denied"))
    with pytest.raises(HostCommandError, match="failed to prepare the compose staging dir"):
        await stage_use_case(parent, "p", [], "")


@pytest.mark.asyncio
async def test_a_timed_out_prepare_is_an_unreachable_host(tmp_path):
    from otto.host.errors import HostUnreachableError

    parent = _parent(tmp_path)
    parent.exec = AsyncMock(
        return_value=CommandResult(Status.Failed, value="", command="", retcode=-1, timed_out=True)
    )
    with pytest.raises(HostUnreachableError, match="failed to prepare the compose staging dir"):
        await stage_use_case(parent, "p", [], "")


@pytest.mark.asyncio
async def test_a_refused_mkdir_for_a_compose_subdir_is_a_command_failure(tmp_path):
    from otto.host.errors import HostCommandError

    parent = _parent(tmp_path)
    # First exec (the top-level prepare) succeeds; the per-file `mkdir -p <n>/` fails.
    parent.exec = AsyncMock(side_effect=[_ok(), _fail("mkdir: no space left on device")])
    with pytest.raises(HostCommandError, match=r"failed to create .*/compose/0"):
        await stage_use_case(
            parent, "p", [ComposeFileToStage("core", "services: {}\n", tmp_path)], ""
        )


@pytest.mark.asyncio
async def test_a_failed_put_for_a_compose_file_is_a_command_failure(tmp_path):
    from otto.host.errors import HostCommandError
    from otto.result import Result as ResultCls

    parent = _parent(tmp_path)
    parent.put = AsyncMock(return_value=ResultCls(Status.Failed, msg="disk full"))
    with pytest.raises(HostCommandError, match="failed to stage"):
        await stage_use_case(
            parent, "p", [ComposeFileToStage("core", "services: {}\n", tmp_path)], ""
        )
