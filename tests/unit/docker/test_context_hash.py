"""Unit tests for the build-context hash used to skip rebuilds."""

from __future__ import annotations

from pathlib import Path

import pytest

from otto.configmodule.repo import DockerImage
from otto.docker._context_hash import context_hash


def _image(context: Path, dockerfile_text: str = "FROM alpine\n", **kw) -> DockerImage:
    df = context / "Dockerfile"
    df.write_text(dockerfile_text)
    return DockerImage(
        name=kw.get("name", "img"),
        dockerfile=df,
        context=context,
        target=kw.get("target"),
        build_args=kw.get("build_args", ()),
    )


def test_hash_is_deterministic(tmp_path):
    (tmp_path / "a.txt").write_text("alpha")
    (tmp_path / "b.txt").write_text("beta")
    img = _image(tmp_path)
    h1 = context_hash(img)
    h2 = context_hash(img)
    assert h1 == h2


def test_hash_changes_on_dockerfile_edit(tmp_path):
    img = _image(tmp_path, dockerfile_text="FROM alpine\nRUN echo a\n")
    h1 = context_hash(img)
    img.dockerfile.write_text("FROM alpine\nRUN echo b\n")
    h2 = context_hash(img)
    assert h1 != h2


def test_hash_changes_on_context_file_edit(tmp_path):
    f = tmp_path / "payload.bin"
    f.write_bytes(b"v1")
    img = _image(tmp_path)
    h1 = context_hash(img)
    f.write_bytes(b"v2")
    h2 = context_hash(img)
    assert h1 != h2


def test_hash_changes_on_build_arg(tmp_path):
    img1 = _image(tmp_path, build_args=())
    img2 = _image(tmp_path, build_args=(("FOO", "1"),))
    assert context_hash(img1) != context_hash(img2)


def test_hash_changes_on_target_stage(tmp_path):
    img1 = _image(tmp_path, target=None)
    img2 = _image(tmp_path, target="prod")
    assert context_hash(img1) != context_hash(img2)


def test_dockerignore_excludes_files_from_hash(tmp_path):
    """Editing an ignored file must NOT change the hash."""
    (tmp_path / ".dockerignore").write_text("ignored.log\n")
    (tmp_path / "kept.txt").write_text("k")
    ignored = tmp_path / "ignored.log"
    ignored.write_text("v1")
    img = _image(tmp_path)
    h1 = context_hash(img)
    ignored.write_text("v2 something quite different")
    h2 = context_hash(img)
    assert h1 == h2


def test_dockerignore_glob_directory(tmp_path):
    (tmp_path / ".dockerignore").write_text("logs/*\n")
    (tmp_path / "logs").mkdir()
    log = tmp_path / "logs" / "out.log"
    log.write_text("v1")
    img = _image(tmp_path)
    h1 = context_hash(img)
    log.write_text("v2")
    h2 = context_hash(img)
    assert h1 == h2
