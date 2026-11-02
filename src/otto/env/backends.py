"""The uv that builds and fills the orchestration venv: the one installed with otto.

ONE INSTALLER. otto depends on the ``uv`` PyPI package, so every otto has a uv
beside it, and :func:`uv_bin` is the only place that locates it. There is no
PATH lookup and no pip fallback: the uv that runs is the one otto's own install
put there, and an install that lacks it is broken and says so
(:class:`UvNotFoundError`).

The path is resolved ONCE per operation by the caller and handed to
:func:`create_venv` and :func:`install`, which never resolve on their own, so
one ``create`` or ``sync`` cannot run two different uv binaries.
"""

import subprocess
import sys
from pathlib import Path

from ..errors import OttoError

_UV_MISSING_MESSAGE = (
    "otto's installation is missing the uv it depends on. Reinstall otto with its "
    "dependencies (e.g. `pip install otto-sh`, or `uv sync` in a "
    "project that depends on it)."
)


class UvNotFoundError(OttoError, FileNotFoundError):
    """The uv that ships with otto is missing or does not run."""


def uv_bin() -> Path:
    """Locate the uv binary installed with otto, or raise :class:`UvNotFoundError`.

    ``uv`` is imported here, not at module top, so nothing that merely imports
    :mod:`otto.env` pays for it. Only a missing ``uv`` package is translated:
    an ``ImportError`` from inside it is a different fault and propagates.
    """
    try:
        from uv import find_uv_bin

        return Path(find_uv_bin())
    except ModuleNotFoundError as exc:
        if exc.name != "uv":
            raise
        raise UvNotFoundError(_UV_MISSING_MESSAGE) from exc
    except FileNotFoundError as exc:
        raise UvNotFoundError(_UV_MISSING_MESSAGE) from exc


def uv_version(uv: Path) -> str:
    """Return the version *uv* reports, or raise :class:`UvNotFoundError` naming it.

    The reported token must parse as a version: it is recorded as the env's
    ``uv_version``, so junk is refused here rather than written there.
    """
    from packaging.version import InvalidVersion, Version

    try:
        proc = _run([str(uv), "--version"])
    except OSError as exc:
        raise UvNotFoundError(f"cannot run uv at {uv}: {exc}") from exc
    if proc.returncode != 0:
        raise UvNotFoundError(f"`{uv} --version` exited {proc.returncode}")
    name, _, rest = proc.stdout.partition(" ")
    version = rest.split()[:1]
    unparseable = UvNotFoundError(
        f"`{uv} --version` printed {proc.stdout.strip()!r}, not `uv <version>`"
    )
    if name != "uv" or not version:
        raise unparseable
    try:
        Version(version[0])
    except InvalidVersion as exc:
        raise unparseable from exc
    return version[0]


def venv_python(env: Path) -> Path:
    """Return the interpreter inside *env*, on either platform layout."""
    if sys.platform == "win32":
        return env / "Scripts" / "python.exe"
    return env / "bin" / "python"


def activation_line(env: Path) -> str:
    """Return the line an operator pastes to enter *env*."""
    if sys.platform == "win32":
        return str(env / "Scripts" / "activate")
    return f"source {env / 'bin' / 'activate'}"


def _run(argv: "list[str]") -> "subprocess.CompletedProcess[str]":
    """Run an installer command, capturing both streams.

    ``check=False`` deliberately: a resolver failure is not an otto crash, it
    is a message otto must RELAY. The caller inspects returncode and passes
    the resolver's own text through verbatim.
    """
    return subprocess.run(  # noqa: S603 — fixed argv led by otto's own uv, no shell
        argv, capture_output=True, text=True, check=False
    )


def create_venv(uv: Path, env: Path) -> "subprocess.CompletedProcess[str]":
    """Create an empty venv at *env* with the uv at *uv*."""
    return _run([str(uv), "venv", str(env)])


def install(
    uv: Path, env: Path, targets: "list[str]", passthrough: "list[str]"
) -> "subprocess.CompletedProcess[str]":
    """Install *targets* into *env* with the uv at *uv*, appending the operator's *passthrough*.

    *targets* are already-formed installer arguments (``-e <path>`` pairs, or a
    plain requirement). *passthrough* is whatever followed ``--`` on the otto
    command line, appended VERBATIM and last so it can override anything otto
    chose -- hermetic index pins are the whole reason it exists.
    """
    argv = [str(uv), "pip", "install", "--python", str(venv_python(env)), *targets]
    return _run([*argv, *passthrough])
