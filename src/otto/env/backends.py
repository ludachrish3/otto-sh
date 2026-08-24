"""Which installer builds and fills the orchestration venv.

TWO BACKENDS, ONE CONTRACT: uv when it is on PATH, stdlib ``venv`` plus the
env's own pip otherwise. The fallback deliberately uses stdlib ``venv`` rather
than the ``virtualenv`` package, so the path taken by users without uv adds no
dependency of its own.

Selection is FLAG > SETTINGS > RECORDED > AUTO-DETECT, and each step exists for
a different reason: the flag is this invocation's decision, the settings key is
the repo's standing decision, the recorded value keeps an existing env
self-consistent (switching backends under an existing env is a
``create --force`` matter, not a silent migration), and auto-detect is what
happens when nobody has said anything.

An explicit request that cannot be honoured is REFUSED, never downgraded: a
``--backend uv`` on a host without uv must not quietly build a pip env, because
the operator asked for uv precisely to avoid that.
"""

import shutil
import subprocess
import sys
from pathlib import Path

from ..errors import OttoError

VALID_BACKENDS = ("uv", "pip")


class BackendUnavailableError(OttoError, RuntimeError):
    """A requested installer backend is unknown or not present on this host."""


def _uv_on_path() -> bool:
    """Whether ``uv`` is callable. Split out so tests can pin both arms."""
    return shutil.which("uv") is not None


def select_backend(
    flag: "str | None",
    settings_value: "str | None",
    recorded: "str | None",
) -> str:
    """Resolve the backend name, or raise :class:`BackendUnavailableError`."""
    for candidate, source in ((flag, "--backend"), (settings_value, "[env] backend")):
        if candidate is None:
            continue
        if candidate not in VALID_BACKENDS:
            raise BackendUnavailableError(
                f"unknown backend {candidate!r} from {source} — valid backends are "
                f"{', '.join(VALID_BACKENDS)}"
            )
        if candidate == "uv" and not _uv_on_path():
            raise BackendUnavailableError(
                f"{source} asked for 'uv', but uv is not on PATH — install uv, or "
                "pass --backend pip to use the stdlib venv + pip fallback"
            )
        return candidate
    if recorded in VALID_BACKENDS:
        return recorded
    return "uv" if _uv_on_path() else "pip"


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
    return subprocess.run(  # noqa: S603 — fixed argv from a validated backend, no shell
        argv, capture_output=True, text=True, check=False
    )


def create_venv(backend: str, env: Path) -> "subprocess.CompletedProcess[str]":
    """Create an empty venv at *env* using *backend*."""
    if backend == "uv":
        return _run(["uv", "venv", str(env)])
    return _run([sys.executable, "-m", "venv", str(env)])


def install(
    backend: str, env: Path, targets: "list[str]", passthrough: "list[str]"
) -> "subprocess.CompletedProcess[str]":
    """Install *targets* into *env*, appending the operator's *passthrough*.

    *targets* are already-formed installer arguments (``-e <path>`` pairs, or a
    plain requirement). *passthrough* is whatever followed ``--`` on the otto
    command line, appended VERBATIM and last so it can override anything otto
    chose -- hermetic index pins are the whole reason it exists.
    """
    if backend == "uv":
        argv = ["uv", "pip", "install", "--python", str(venv_python(env)), *targets]
    else:
        argv = [str(venv_python(env)), "-m", "pip", "install", *targets]
    return _run([*argv, *passthrough])
