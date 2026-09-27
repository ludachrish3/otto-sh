"""The per-workspace orchestration venv behind ``otto env``.

:mod:`otto.env.manage` locates, builds, syncs and describes the venv and the
metadata it records; :mod:`otto.env.backends` is the installer that fills it
(uv, or stdlib ``venv`` plus pip); :mod:`otto.env.preflight` checks each
repo's declared requirements against the running environment.

Every name is exported lazily (PEP 562), the shape every otto package shares:
the preflight, which runs on the path of every ordinary command, loads
without the venv builder and its subprocess and installer machinery. The
resolver does not write a resolved name back into the module dict; see
``otto.config``'s ``__dir__`` for why.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backends import BackendUnavailableError as BackendUnavailableError
    from .manage import META_FILENAME as META_FILENAME
    from .manage import EnvBuild as EnvBuild
    from .manage import EnvBuildError as EnvBuildError
    from .manage import EnvExistsError as EnvExistsError
    from .manage import EnvMeta as EnvMeta
    from .manage import EnvStatus as EnvStatus
    from .manage import RepoState as RepoState
    from .manage import create_env as create_env
    from .manage import env_path as env_path
    from .manage import env_status as env_status
    from .manage import meta_path as meta_path
    from .manage import read_meta as read_meta
    from .manage import sync_env as sync_env
    from .manage import write_meta as write_meta

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "BackendUnavailableError": "otto.env.backends",
    "EnvBuild": "otto.env.manage",
    "EnvBuildError": "otto.env.manage",
    "EnvExistsError": "otto.env.manage",
    "EnvMeta": "otto.env.manage",
    "EnvStatus": "otto.env.manage",
    "META_FILENAME": "otto.env.manage",
    "RepoState": "otto.env.manage",
    "create_env": "otto.env.manage",
    "env_path": "otto.env.manage",
    "env_status": "otto.env.manage",
    "meta_path": "otto.env.manage",
    "read_meta": "otto.env.manage",
    "sync_env": "otto.env.manage",
    "write_meta": "otto.env.manage",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.env's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "META_FILENAME",
    "BackendUnavailableError",
    "EnvBuild",
    "EnvBuildError",
    "EnvExistsError",
    "EnvMeta",
    "EnvStatus",
    "RepoState",
    "create_env",
    "env_path",
    "env_status",
    "meta_path",
    "read_meta",
    "sync_env",
    "write_meta",
]
