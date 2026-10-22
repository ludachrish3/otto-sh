"""A ``[[docker.images]]`` entry whose Dockerfile or context is missing is a config error (#674).

Before, the build tarred the context and then the Dockerfile, and a missing
file surfaced as a ``FileNotFoundError`` traceback out of ``tarfile``. Now the
declared paths are checked before anything is tarred or sent, and the refusal
is one line naming the image, the settings key and the resolved path.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.config.scope import DockerImage
from otto.docker.build import DockerImageConfigError, build_images
from otto.errors import OttoError
from otto.result import CommandResult
from otto.utils import Status
from tests._fixtures.fake_project import fake_project

pytestmark = pytest.mark.asyncio


def _parent() -> MagicMock:
    parent = MagicMock()
    parent.id = "test3"
    parent.exec = AsyncMock(
        return_value=CommandResult(Status.Success, value="", command="x", retcode=0)
    )
    parent.put = AsyncMock()
    return parent


def _project(*images: DockerImage):
    return fake_project(
        "mix", docker_settings=SimpleNamespace(images=images, composes=(), use_cases=())
    )


async def test_a_missing_dockerfile_is_one_config_error_naming_image_key_and_path(tmp_path):
    image = DockerImage(name="stack", dockerfile=tmp_path / "Dockerfile", context=tmp_path)
    parent = _parent()

    with pytest.raises(DockerImageConfigError) as caught:
        await build_images(_project(image), parent)

    message = str(caught.value)
    assert "\n" not in message
    assert "'stack'" in message
    assert "dockerfile" in message
    assert str(tmp_path / "Dockerfile") in message
    assert isinstance(caught.value, OttoError)
    # Refused before anything reached the parent.
    parent.exec.assert_not_awaited()
    parent.put.assert_not_awaited()


async def test_a_missing_context_is_one_config_error_naming_the_context_key(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM alpine\n")
    missing = tmp_path / "no-such-dir"
    image = DockerImage(name="stack", dockerfile=tmp_path / "Dockerfile", context=missing)

    with pytest.raises(DockerImageConfigError) as caught:
        await build_images(_project(image), _parent())

    message = str(caught.value)
    assert "\n" not in message
    assert "'stack'" in message
    assert "context" in message
    assert str(missing) in message


async def test_a_context_that_is_a_file_but_not_an_archive_is_refused(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM alpine\n")
    not_a_dir = tmp_path / "ctx.txt"
    not_a_dir.write_text("x")
    image = DockerImage(name="stack", dockerfile=tmp_path / "Dockerfile", context=not_a_dir)

    with pytest.raises(DockerImageConfigError, match="not a directory"):
        await build_images(_project(image), _parent())


async def test_an_archive_context_is_not_asked_for_a_dockerfile_on_disk(tmp_path):
    archive = tmp_path / "ctx.tar"
    image = DockerImage(
        name="stack",
        dockerfile=tmp_path / "Dockerfile",  # a path INSIDE the archive; not on disk
        context=archive,
        dockerfile_in_archive="Dockerfile",
    )
    # The archive itself must exist ...
    with pytest.raises(DockerImageConfigError, match="context"):
        await build_images(_project(image), _parent())
    # ... and once it does, the missing on-disk Dockerfile is not a refusal.
    archive.write_bytes(b"")
    from otto.docker.build import check_declared_paths

    check_declared_paths("mix", image)


async def test_only_the_selected_images_are_checked(tmp_path):
    good = tmp_path / "good"
    good.mkdir()
    (good / "Dockerfile").write_text("FROM alpine\n")
    broken = DockerImage(name="broken", dockerfile=tmp_path / "nope", context=tmp_path)
    fine = DockerImage(name="fine", dockerfile=good / "Dockerfile", context=good)

    from otto.docker.build import check_declared_paths

    check_declared_paths("mix", fine)
    with pytest.raises(DockerImageConfigError):
        check_declared_paths("mix", broken)
    # Selecting no image of a project with a broken entry is not this build's problem.
    assert await build_images(_project(broken, fine), _parent(), image_names=[]) == {}
