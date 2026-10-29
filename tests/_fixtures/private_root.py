"""Docker parents whose private staging root otto can trust, for tests about something else.

Docker staging reads the type, owner and mode of the parent's private root
(:func:`otto.host.docker_host.probe_ownership`) before every write under it and
every read-back from it. A unit test whose parent's ``exec`` is a blanket mock
cannot answer that probe, and is not about it. Two fixtures answer it the way a
trustworthy parent would:

- :func:`trusted_private_roots` replaces the probe: ``/tmp`` is a root-owned
  sticky directory, every ``/tmp/otto-docker-<login>`` is a directory owned by
  :data:`SESSION_UID` with mode 0700, and nothing else is there (so no older
  staging is found under ``/tmp/otto-docker``). The real root path is still
  derived from the parent's creds, so the parent must be a
  :class:`~otto.host.unix_host.UnixHost`.
- :func:`trusted_container_staging` is for a container whose parent is a
  mock rather than a unix host: it replaces the whole check and answers
  :data:`CONTAINER_STAGING_ROOT`.

The check itself is pinned without either fixture, against parents that answer
the real probe command through :class:`ParentPaths`:
``tests/unit/host/test_private_staging_root.py`` and the legacy read-back tests
in ``tests/unit/docker/test_deploy.py``.
"""

import re
import shlex
from pathlib import Path

import pytest

from otto.host import docker_host
from otto.host.docker_host import OwnershipProbe, PathOwnership
from otto.result import CommandResult
from otto.utils import Status

SESSION_UID = 1000
"""The UID every fake parent's command session runs as."""

CONTAINER_STAGING_ROOT = Path("/tmp/otto-docker-vagrant")
"""The root :func:`trusted_container_staging` answers for every parent."""

_TMP = Path("/tmp")


def trustworthy(paths: "list[Path]") -> OwnershipProbe:
    """Answer :func:`~otto.host.docker_host.probe_ownership` as a trustworthy parent would."""
    found: "dict[Path, PathOwnership | None]" = {}
    for path in paths:
        if path == _TMP:
            found[path] = PathOwnership(kind="directory", uid=0, mode=0o1777)
        elif path.parent == _TMP and path.name.startswith("otto-docker-"):
            found[path] = PathOwnership(kind="directory", uid=SESSION_UID, mode=0o700)
        else:
            found[path] = None
    return OwnershipProbe(uid=SESSION_UID, paths=found, unreadable=[])


class ParentPaths:
    """Answers otto's real ownership probe from a table of paths, as the parent's shell would.

    *paths* maps a path to what ``stat -c '%F %u %a'`` prints for it; a path
    not in the table is not there, and ``/tmp`` is a root-owned sticky
    directory unless the table says otherwise. The probe's ``mkdir -m 0700 P``
    adds P, owned by *uid*, only when P is absent, which is what ``mkdir``
    does.
    """

    def __init__(self, paths: "dict[Path, str] | None" = None, uid: int = SESSION_UID) -> None:
        self.paths: "dict[Path, str]" = {_TMP: "directory 0 1777", **(paths or {})}
        self.uid = uid

    def answer(self, command: str) -> "CommandResult | None":
        """The parent's answer to *command* if it is the ownership probe, else ``None``."""
        probed = re.search(r"; for p in (.*?); do ", command)
        if probed is None or "id -u" not in command:
            return None
        made = re.match(r"mkdir -m 0700 (.+?) 2>/dev/null; ", command)
        if made is not None:
            self.paths.setdefault(Path(shlex.split(made[1])[0]), f"directory {self.uid} 700")
        lines = [str(self.uid)]
        lines += [self.paths.get(Path(p), "missing") for p in shlex.split(probed[1])]
        return CommandResult(Status.Success, value="\n".join(lines) + "\n", command=command)


@pytest.fixture
def trusted_private_roots(monkeypatch):
    """Every parent's private staging root is trustworthy; no older staging is there."""
    # Function-scope: tests/unit/host's conftest imports this module, and a
    # host test must not pull the docker package in at collection.
    from otto.docker import staging

    async def _probe(_parent, paths, *, create=None):
        return trustworthy(list(paths))

    # Both names: `ensure_private_root` reads the host module's, and the
    # legacy read-back (`trusted_legacy_staging`) the name staging imported.
    monkeypatch.setattr(docker_host, "probe_ownership", _probe)
    monkeypatch.setattr(staging, "probe_ownership", _probe)


@pytest.fixture
def trusted_container_staging(monkeypatch):
    """A container's transfers stage under :data:`CONTAINER_STAGING_ROOT`, whatever its parent."""

    async def _root(_parent):
        return CONTAINER_STAGING_ROOT

    monkeypatch.setattr(docker_host, "ensure_private_root", _root)
