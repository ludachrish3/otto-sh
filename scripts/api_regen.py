"""Generate an API dump from one commit, the way the checker trusts it.

Spec: ``docs/superpowers/specs/2026-10-05-api-dump-design.md`` §5. The checker
never reads the working tree: :func:`generate_at_commit` extracts from ``git
archive <sha>`` only what regeneration reads (``src/``, ``pyproject.toml``,
``uv.lock`` and the manifest), resolves that commit's dependencies into a cached
virtualenv keyed by its lock (:class:`UvDepsEnv`; otto itself is never
installed), and runs ``scripts/api_dump_child.py`` with the archive's ``src``
first on ``sys.path``, under an allowlisted environment, with a hard timeout.
:func:`generate_worktree` is the developer loop: the same child, on the working
tree, in the developer's own interpreter.
"""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import tomli

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scripts import api_records  # noqa: E402 -- path set up above
from scripts.api_manifest import (  # noqa: E402
    Format,
    ManifestError,
    load_formats,
    load_manifest,
    parse_formats,
    parse_manifest,
)

CHILD = REPO_ROOT / "scripts" / "api_dump_child.py"
GATE_HASH_SEED = "0"
CHILD_TIMEOUT = 300
SYNC_TIMEOUT = 1200
LOCK_CHECK_TIMEOUT = 120
# What regeneration reads from a commit, besides its manifest.
ARCHIVE_INPUTS = ("src", "pyproject.toml", "uv.lock")
_KEYED_PROJECT_FIELDS = (
    "name",
    "version",
    "dependencies",
    "optional-dependencies",
    "requires-python",
)


def _tool(name: str) -> str:
    """Return the resolved path of the executable *name*, or *name* if absent."""
    return shutil.which(name) or name


class EnvError(Exception):
    """The commit's environment or the child failed; not a fact about the API."""


@dataclass
class Generated:
    """A generated dump, or why there is none."""

    text: "str | None"
    refusals: "list[str]" = field(default_factory=list)
    private: "dict[str, list[str]]" = field(default_factory=dict)


def child_env(home: Path, seed: str) -> "dict[str, str]":
    """Return the child's whole environment: an allowlist, nothing inherited (§5.3)."""
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONHASHSEED": seed,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }


def run_child(
    python: Path,
    src: Path,
    namespaces: "list[str]",
    *,
    seed: str = GATE_HASH_SEED,
    assume_dir: bool = False,
    timeout: float = CHILD_TIMEOUT,
    formats: "list[Format] | None" = None,
) -> "dict[str, Any]":
    """Run the producer child with *python* on *src*; return its JSON report."""
    argv = [str(python), str(CHILD), "--src", str(src)]
    if assume_dir:
        argv.append("--assume-dir")
    for fmt in formats or []:
        argv += [
            "--format",
            fmt.name,
            fmt.reads or api_records.EMPTY,
            fmt.writes or api_records.EMPTY,
        ]
    with tempfile.TemporaryDirectory(prefix="otto-api-dump-") as tmp:
        proc = subprocess.Popen(  # noqa: S603 -- fixed argv, no shell
            [*argv, *namespaces],
            cwd=tmp,
            env=child_env(Path(tmp), seed),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise EnvError(f"the dump child timed out after {timeout:g}s") from exc
    if proc.returncode != 0:
        raise EnvError(f"the dump child crashed (exit {proc.returncode}): {err.strip()[-2000:]}")
    try:
        report = json.loads(out.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise EnvError(f"the dump child printed no report: {exc}") from exc
    _check_report(report)
    return report


def _check_report(report: object) -> None:
    """Raise :class:`EnvError` unless *report* has the child's four-field shape."""
    if not isinstance(report, dict):
        raise EnvError(f"the dump child's report is malformed: not an object ({report!r:.80})")
    for field_name in ("records", "refusals", "provenance"):
        value = report.get(field_name)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise EnvError(
                f"the dump child's report is malformed: {field_name!r} is not a list of strings"
            )
    if not isinstance(report.get("private"), dict):
        raise EnvError("the dump child's report is malformed: 'private' is not an object")


def _generated(report: dict) -> Generated:
    refusals = [*report["refusals"], *(f"provenance: {p}" for p in report["provenance"])]
    if refusals:
        return Generated(None, refusals, report["private"])
    try:
        records = [api_records.parse_record(line) for line in report["records"]]
        text = api_records.render_dump(records)
        api_records.parse_dump(text)
    except api_records.DumpError as exc:
        return Generated(None, [f"the producer's output does not parse: {exc}"])
    return Generated(text, [], report["private"])


class DepsEnv(Protocol):
    """Anything that names the interpreter to run a commit's child under."""

    def python(self, tree: Path) -> Path:
        """Return the interpreter for the archive extracted at *tree*."""
        ...


class CurrentEnv:
    """The running interpreter: the developer loop and the tests."""

    def python(self, tree: Path) -> Path:
        """Return ``sys.executable``, whatever *tree* locks."""
        del tree
        return Path(sys.executable)


class UvDepsEnv:
    """One virtualenv per distinct dependency set, built from a commit's own lock (§5.2)."""

    def __init__(self, cache_root: "Path | None" = None) -> None:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        self.root = cache_root or base / "otto-api-dump"

    def _key(self, tree: Path) -> str:
        project = tomli.loads((tree / "pyproject.toml").read_text(encoding="utf-8"))
        meta = project.get("project", {})
        parts = {
            "project": {k: meta.get(k) for k in _KEYED_PROJECT_FIELDS},
            "dependency-groups": project.get("dependency-groups"),
            "tool.uv": project.get("tool", {}).get("uv"),
            "lock": hashlib.sha256((tree / "uv.lock").read_bytes()).hexdigest(),
            "python": sys.version,
            "platform": sysconfig.get_platform(),
            "uv": _uv_version(),
        }
        blob = json.dumps(parts, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:32]

    def _check_lock(self, tree: Path) -> None:
        """Refuse a lock ``uv sync --locked`` would reject, on a hit as well as a miss."""
        try:
            proc = subprocess.run(  # noqa: S603 -- fixed argv
                [_tool("uv"), "lock", "--check", "--offline", "--python", sys.executable],
                cwd=tree,
                env=_uv_env(),
                capture_output=True,
                text=True,
                timeout=LOCK_CHECK_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EnvError(f"uv lock --check could not run: {exc}") from exc
        if proc.returncode != 0:
            raise EnvError(f"uv lock --check failed: {proc.stderr.strip()[-2000:]}")

    def python(self, tree: Path) -> Path:
        """Return the cached interpreter for *tree*'s lock, building it on a miss."""
        try:
            key = self._key(tree)
        except (OSError, tomli.TOMLDecodeError) as exc:
            raise EnvError(f"cannot read this commit's pyproject.toml/uv.lock: {exc}") from exc
        self._check_lock(tree)
        venv = self.root / key
        python = venv / "bin" / "python"
        if _usable(python):
            return python
        shutil.rmtree(venv, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
        partial = self.root / f"{key}.partial-{os.getpid()}"
        shutil.rmtree(partial, ignore_errors=True)
        try:
            self._sync(tree, partial)
            try:
                partial.replace(venv)
            except OSError as exc:
                if not _usable(python):  # a concurrent builder won only if its result works
                    raise EnvError(f"cannot promote the built environment: {exc}") from exc
        finally:
            shutil.rmtree(partial, ignore_errors=True)
        return python

    def _sync(self, tree: Path, partial: Path) -> None:
        env = _uv_env()
        env["UV_PROJECT_ENVIRONMENT"] = str(partial)
        try:
            proc = subprocess.run(  # noqa: S603 -- fixed argv
                [
                    _tool("uv"),
                    "sync",
                    "--locked",
                    "--no-install-project",
                    "--no-default-groups",
                    "--python",
                    sys.executable,
                ],
                cwd=tree,
                env=env,
                capture_output=True,
                text=True,
                timeout=SYNC_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise EnvError(f"uv sync timed out after {SYNC_TIMEOUT:g}s") from exc
        if proc.returncode != 0:
            raise EnvError(
                f"uv sync --locked failed for this commit: {proc.stderr.strip()[-2000:]}"
            )


# Inherited variables that would point uv at another project, lock, environment
# or working directory, or switch `--locked` validation off, without the cache
# key seeing them (from `uv help sync` / `uv help lock`; UV_PROJECT_ENVIRONMENT
# is set explicitly where a sync needs it). Cache, index, network and offline
# settings pass through: an air-gapped install relies on them.
UV_TARGET_VARS = frozenset(
    {
        "VIRTUAL_ENV",
        "UV_PROJECT",
        "UV_WORKING_DIR",
        "UV_PROJECT_ENVIRONMENT",
        "UV_LOCKED",
        "UV_FROZEN",
    }
)


def _uv_env() -> "dict[str, str]":
    """Return the ambient environment minus every :data:`UV_TARGET_VARS` entry."""
    return {k: v for k, v in os.environ.items() if k not in UV_TARGET_VARS}


def _uv_version() -> str:
    """Return ``uv --version``; raise :class:`EnvError` if uv is missing or unusable."""
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv
            [_tool("uv"), "--version"],
            capture_output=True,
            text=True,
            timeout=LOCK_CHECK_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EnvError(f"uv not found/unusable: {exc}") from exc
    version = proc.stdout.strip()
    if proc.returncode != 0 or not version:
        raise EnvError("uv not found/unusable: `uv --version` printed nothing")
    return version


def _usable(python: Path) -> bool:
    """Return whether *python* exists and starts."""
    if not python.exists():
        return False
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv
            [str(python), "-c", "pass"],
            capture_output=True,
            timeout=LOCK_CHECK_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def archive_commit(repo: Path, sha: str, dest: Path, manifest_rel: "str | None" = None) -> None:
    """Extract the committed inputs of regeneration at *sha* into *dest*.

    Only :data:`ARCHIVE_INPUTS` and the manifest at *manifest_rel* are extracted,
    never the whole tree: every extracted file costs file operations, and nothing
    else is read. ``git archive`` refuses a pathspec the commit lacks, so each
    path is first looked up in the commit and an absent one is left out; the
    reader of that path then fails exactly as it would on a whole-tree archive.
    A dependency the lock locates outside these paths (a path source) is absent
    too, and ``uv`` refuses the lock: loud, never a wrong dump.
    """
    wanted = [*ARCHIVE_INPUTS]
    if manifest_rel and not PurePosixPath(manifest_rel).is_absolute():
        wanted.append(manifest_rel)
    present = _paths_in_commit(repo, sha, wanted)
    if not present:
        return
    proc = subprocess.Popen(  # noqa: S603 -- fixed argv
        [_tool("git"), "--literal-pathspecs", "archive", "--format=tar", sha, "--", *present],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_vcs_env(),
    )
    with proc:
        try:
            _extract(proc, dest)
        except tarfile.TarError as exc:
            proc.kill()  # never wait on a producer blocked on a pipe nobody reads
            problem = f"unreadable archive: {exc}"
        else:
            problem = ""
        code = proc.wait()
        message = proc.stderr.read().decode().strip() if proc.stderr else ""
    if code != 0 or problem:
        raise EnvError(f"git archive {sha[:12]} failed: {message or problem}")


def _paths_in_commit(repo: Path, sha: str, paths: "list[str]") -> "list[str]":
    """Return the entries of *paths* that exist at *sha*, as git spells them."""
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv
            [
                _tool("git"),
                "--literal-pathspecs",
                "ls-tree",
                "-z",
                "--name-only",
                sha,
                "--",
                *paths,
            ],
            cwd=repo,
            capture_output=True,
            env=_vcs_env(),
            timeout=LOCK_CHECK_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EnvError(f"git ls-tree {sha[:12]} could not run: {exc}") from exc
    if proc.returncode != 0:
        raise EnvError(f"git ls-tree {sha[:12]} failed: {proc.stderr.decode().strip()}")
    return [p.decode() for p in proc.stdout.split(b"\0") if p]


def _vcs_env() -> "dict[str, str]":
    """Return the environment for git: no repository-location variables, no user config."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    return env


def _extract(proc: "subprocess.Popen[bytes]", dest: Path) -> None:
    """Extract the tar stream on *proc*'s stdout into *dest*."""
    with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
        else:  # pragma: no cover -- an interpreter without PEP 706
            tar.extractall(dest)  # noqa: S202 -- our own repository's archive


def generate_at_commit(repo: Path, sha: str, manifest_rel: str, env: "DepsEnv") -> Generated:
    """Generate *sha*'s dump from its archive, its manifest and its dependencies."""
    with tempfile.TemporaryDirectory(prefix="otto-api-archive-") as tmp:
        tree = Path(tmp)
        try:
            archive_commit(repo, sha, tree, manifest_rel)
        except EnvError as exc:
            return Generated(None, [f"environment: {exc}"])
        try:
            # Bytes, decoded strictly: no newline translation, a non-UTF-8 file refused.
            text = (tree / manifest_rel).read_bytes().decode("utf-8")
            namespaces = sorted(parse_manifest(text))
            formats = list(parse_formats(text).values())
        except (OSError, UnicodeDecodeError, ManifestError) as exc:
            return Generated(None, [f"manifest: {exc}"])
        try:
            report = run_child(env.python(tree), tree / "src", namespaces, formats=formats)
        except EnvError as exc:
            return Generated(None, [f"environment: {exc}"])
        return _generated(report)


def generate_worktree(repo: Path, manifest: Path, *, assume_dir: bool = False) -> Generated:
    """Generate the working tree's dump in this interpreter: the developer loop (§5.1)."""
    try:
        namespaces = sorted(load_manifest(manifest))
        formats = list(load_formats(manifest).values())
    except ManifestError as exc:
        return Generated(None, [f"manifest: {exc}"])
    try:
        report = run_child(
            Path(sys.executable),
            Path(repo) / "src",
            namespaces,
            assume_dir=assume_dir,
            formats=formats,
        )
    except EnvError as exc:
        return Generated(None, [f"environment: {exc}"])
    return _generated(report)
