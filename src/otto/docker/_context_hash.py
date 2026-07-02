"""
Build-context hashing for Docker image build skipping.

The hash covers everything that should change the resulting image:

- Dockerfile bytes
- Every file inside the build context, after applying ``.dockerignore``,
  along with its relative path
- Build args (sorted by key)
- Optional multi-stage target stage

The result is used as part of the image tag (``<image>:<hash>``) so a
build is skipped when ``docker image inspect <image>:<hash>`` succeeds.
This is correct even when the same image is needed on a different parent
host — the hash only depends on inputs, not on the build host.
"""

import fnmatch
import hashlib
from collections.abc import Iterable
from pathlib import Path

from ..configmodule.repo import DockerImage


def _read_dockerignore(context: Path) -> list[str]:
    ignore_path = context / ".dockerignore"
    if not ignore_path.is_file():
        return []
    patterns: list[str] = []
    for line in ignore_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        patterns.append(stripped)
    return patterns


def _is_ignored(rel: str, patterns: list[str]) -> bool:
    """Match *rel* (forward-slash relative path) against .dockerignore patterns.

    Implementation is intentionally simple: glob match against the full
    relative path and against every leading directory of it. Negation
    patterns (``!foo``) are not supported in the hash; a project that
    relies on them should override ``--rebuild`` rather than trust the
    hash result.
    """
    if not patterns:
        return False
    parts = rel.split("/")
    candidates = [rel] + ["/".join(parts[: i + 1]) for i in range(len(parts))]
    for pat in patterns:
        if pat.startswith("!"):
            continue
        for c in candidates:
            if fnmatch.fnmatch(c, pat):
                return True
    return False


def _walk_context(context: Path) -> Iterable[tuple[str, Path]]:
    patterns = _read_dockerignore(context)
    for path in sorted(context.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(context).as_posix()
        if _is_ignored(rel, patterns):
            continue
        yield rel, path


def context_hash(image: DockerImage) -> str:
    """Return a stable sha256 hex digest of *image*'s build inputs."""
    h = hashlib.sha256()

    h.update(b"dockerfile:")
    h.update(image.dockerfile.read_bytes())
    h.update(b"\n")

    for arg_name, arg_value in image.build_args:
        h.update(f"arg:{arg_name}={arg_value}\n".encode())

    if image.target:
        h.update(f"target:{image.target}\n".encode())

    for rel, path in _walk_context(image.context):
        h.update(f"file:{rel}\n".encode())
        h.update(hashlib.sha256(path.read_bytes()).digest())

    return h.hexdigest()
