#!/usr/bin/env python3
"""Refresh or check the committed command captures the Getting Started pages include.

Spec 2026-08-27 §5. A TOML manifest lists each capture: the argv to run, whether
it needs the bed, which example project to run it against (copied into the
scratch dir on first use, so every path a capture prints is anchored to the
scratch dir rather than baking in this checkout's own path — see
``RunContext.project_dir``), the exit code it is expected to return, the
seconds it may run before it is killed, directories to make under the scratch
dir before it runs, a settings fragment to append to the project copy for that
capture alone, and redaction rules — ``id``, ``argv``, ``labless``,
``project``, ``expect_exit``, ``timeout``, ``mkdir``, ``settings_append``,
``redact``.
The runner executes each one in a clean environment, redacts
the output, and either writes
``captures/<id>.txt`` (refresh) or diffs against it (``--check``, exit 1 on
drift). ``--labless`` selects only captures that need no bed; the docs gate
runs ``--check --labless``, and ``make docs-captures-check`` runs ``--check``
against the bed.

Deliberately a script, not a pytest module: nothing collected under
tests/integration/ may run without reaping the lab, and this must be safe to
invoke while unrelated work is in flight.

Captures run in a fixed scratch directory (``SCRATCH_DIR``), not a fresh
``tempfile.mkdtemp()``: a Rich-rendered table wraps and centres its title
against the *pre-redaction* scratch path, so the artifact's layout depends on
that path's length. A directory that moves with ``$TMPDIR`` freezes a
layout that only holds on the machine (and environment) that took it. Because
the scratch directory is fixed and reused, two refreshes must not run
concurrently on one machine.
"""

import argparse
import contextlib
import dataclasses
import difflib
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import tomli

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "docs" / "examples"
DEFAULT_MANIFEST = EXAMPLES / "getting-started" / "captures.toml"
DEFAULT_CAPTURES = EXAMPLES / "getting-started" / "captures"
SCRATCH_DIR = Path("/tmp/otto-gs")  # noqa: S108 -- a fixed, documented scratch path; the artifact layout depends on its length

_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")


class CaptureError(RuntimeError):
    """A capture could not be taken as the manifest describes."""


@dataclasses.dataclass(frozen=True)
class Capture:
    """One manifest entry: the argv to run and how to judge and redact it."""

    id: str
    argv: list[str]
    labless: bool = False
    project: str = ""
    expect_exit: int = 0
    timeout: int = 120
    redact: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    mkdir: list[str] = dataclasses.field(default_factory=list)
    settings_append: str = ""


@dataclasses.dataclass
class RunContext:
    """Where a run reads and writes: the example projects, the artifacts, and a scratch dir."""

    examples_root: Path
    captures_dir: Path
    tmp: Path
    _project_dirs: dict[str, Path] = dataclasses.field(init=False, default_factory=dict, repr=False)

    def project_dir(self, cap: Capture) -> Path:
        """Return the scratch-dir copy of ``cap.project``, copying it there on first use.

        A capture must never point ``OTTO_SUT_DIRS`` (or any printed path) at
        ``examples_root`` directly: that path is under this checkout, and its
        length varies by worktree, which shifts exactly where Rich wraps a
        long log line -- baking a checkout-specific wrap point into the
        committed artifact. Copying the project into the scratch dir first
        means every printed path is anchored to ``SCRATCH_DIR`` instead, which
        is fixed and identical on every checkout. Cached by project name on
        ``self`` so two captures sharing a project copy it only once per run.
        """
        if cap.project not in self._project_dirs:
            src = self.examples_root / cap.project
            if not src.is_dir():
                raise CaptureError(
                    f"capture {cap.id!r}: project {cap.project!r} "
                    f"not found under {self.examples_root}"
                )
            dst = self.tmp / cap.project
            shutil.copytree(src, dst, dirs_exist_ok=True)
            self._project_dirs[cap.project] = dst
        return self._project_dirs[cap.project]

    def default_rules(self) -> list[tuple[str, str]]:
        """Redaction rules every capture gets before its own manifest rules run."""
        # Order matters: the scratch dir is redacted before the examples
        # root (so a path never half-matches a date later), the examples
        # root before the repo root (an examples root under ROOT is a
        # substring of it and must win), and the repo root before the
        # generic timestamp rule.
        return [
            (re.escape(str(self.tmp)), str(SCRATCH_DIR)),
            (re.escape(str(self.examples_root)), "<examples>"),
            (re.escape(str(ROOT)), "<repo>"),
            (_TIMESTAMP.pattern, "<timestamp>"),
        ]

    def substitutions(self, cap: Capture) -> dict[str, str]:
        """Build the ``{placeholder}`` -> real-path mapping for a capture's actual argv."""
        otto = Path(sys.executable).parent / "otto"
        project = str(self.project_dir(cap)) if cap.project else ""
        return {
            "{otto}": str(otto),
            "{python}": sys.executable,
            "{tmp}": str(self.tmp),
            "{project}": project,
            "{repo}": str(ROOT),  # captures run from {tmp}; repo scripts need an absolute path
        }

    def env(self, cap: Capture) -> dict[str, str]:
        """Build the clean environment a capture's subprocess runs in."""
        # Same shape as scripts/capture_docs_termynal.py: strip every OTTO_*
        # variable, pin width and colour, then point at the example project.
        # The width differs on purpose -- termynal renders into an 80-column
        # player; these artifacts are read as text in a page, so 100.
        env = {k: v for k, v in os.environ.items() if not k.startswith("OTTO_")}
        home = self.tmp / "home"
        home.mkdir(parents=True, exist_ok=True)
        env.update(
            NO_COLOR="1",
            TERM="dumb",
            COLUMNS="100",
            LINES="50",
            OTTO_HOME=str(home),
            OTTO_XDIR=str(self.tmp / "xdir"),
        )
        (self.tmp / "xdir").mkdir(parents=True, exist_ok=True)
        if cap.project:
            env["OTTO_SUT_DIRS"] = str(self.project_dir(cap))
        return env


def load_manifest(path: Path) -> list[Capture]:
    """Parse the TOML manifest at ``path`` into a list of Captures."""
    try:
        doc = tomli.loads(path.read_text())
    except (OSError, tomli.TOMLDecodeError) as exc:
        raise CaptureError(f"manifest {path}: {exc}") from exc
    caps = []
    for n, raw in enumerate(doc.get("capture", []), start=1):
        for key in ("id", "argv"):
            if key not in raw:
                raise CaptureError(f"manifest {path}: capture #{n} lacks {key!r}")
        rules = []
        for entry in raw.get("redact", []):
            try:
                pattern, repl = entry
            except (TypeError, ValueError) as exc:
                raise CaptureError(
                    f"manifest {path}: capture {raw['id']!r}: "
                    "redact entries are [pattern, replacement] pairs"
                ) from exc
            rules.append((pattern, repl))
        raw_mkdir = raw.get("mkdir", [])
        if not isinstance(raw_mkdir, list) or not all(isinstance(d, str) for d in raw_mkdir):
            raise CaptureError(f"manifest {path}: capture {raw['id']!r}: mkdir entries are strings")
        raw_append = raw.get("settings_append", "")
        if not isinstance(raw_append, str):
            raise CaptureError(
                f"manifest {path}: capture {raw['id']!r}: "
                "settings_append is a file name (relative to the project)"
            )
        if raw_append and not raw.get("project"):
            raise CaptureError(
                f"manifest {path}: capture {raw['id']!r}: settings_append needs project"
            )
        caps.append(
            Capture(
                id=raw["id"],
                argv=list(raw["argv"]),
                labless=bool(raw.get("labless", False)),
                project=raw.get("project", ""),
                expect_exit=int(raw.get("expect_exit", 0)),
                timeout=int(raw.get("timeout", 120)),
                redact=rules,
                mkdir=list(raw_mkdir),
                settings_append=raw_append,
            )
        )
    ids = [c.id for c in caps]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise CaptureError(f"duplicate capture ids: {dupes}")
    return caps


def select(caps: list[Capture], *, labless: bool, only: list[str]) -> list[Capture]:
    """Narrow ``caps`` to the requested ids, then to labless ones if asked."""
    known = {c.id for c in caps}
    for wanted in only:
        if wanted not in known:
            raise CaptureError(f"unknown capture id: {wanted}")
    chosen = [c for c in caps if (not only or c.id in only)]
    if labless:
        chosen = [c for c in chosen if c.labless]
    return chosen


def redact(text: str, rules: list[tuple[str, str]]) -> str:
    """Apply each ``(pattern, replacement)`` rule to ``text`` in order."""
    for pattern, repl in rules:
        text = re.sub(pattern, repl, text)
    return text


def render_command(argv: list[str]) -> str:
    """Render the first line of every artifact: the command as a reader would type it."""
    shown = {
        "{otto}": "otto",
        "{python}": "python",
        "{tmp}": str(SCRATCH_DIR),
        "{project}": ".",
        "{repo}": ".",
    }
    rendered = []
    for arg in argv:
        line = arg
        for key, value in shown.items():
            line = line.replace(key, value)
        rendered.append(line)
    return "$ " + " ".join(rendered)


@contextlib.contextmanager
def _appended_settings(cap: Capture, ctx: RunContext) -> Iterator[None]:
    """Append ``cap.settings_append`` to the project copy's settings for one run.

    Reservation identity is ``--as-user`` or the login name -- nothing an
    environment can pin -- so the committed example must not select a backend
    or every other capture (and every reader) would be refused. A capture that
    needs the selection appends it here. The scratch copy is shared by every
    capture of the same project in a run, so the original bytes are put back
    afterwards, even when the command fails. The containment check below sees
    the copy ``copytree`` made, which dereferences symlinks, so a committed
    symlink out of the project is no weaker than a committed file.
    """
    if not cap.settings_append:
        yield
        return
    if not cap.project:
        raise CaptureError(
            f"capture {cap.id!r}: settings_append {cap.settings_append!r} needs project"
        )
    project = ctx.project_dir(cap)
    fragment = (project / cap.settings_append).resolve()
    if not fragment.is_relative_to(project.resolve()) or not fragment.is_file():
        raise CaptureError(
            f"capture {cap.id!r}: settings_append {cap.settings_append!r} "
            f"is not a file inside project {cap.project!r}"
        )
    settings = project / ".otto" / "settings.toml"
    try:
        original = settings.read_bytes()
    except OSError as exc:
        raise CaptureError(f"capture {cap.id!r}: cannot read {settings}: {exc}") from exc
    settings.write_bytes(original + b"\n" + fragment.read_bytes())
    try:
        yield
    finally:
        settings.write_bytes(original)


def run_capture(cap: Capture, ctx: RunContext) -> str:
    """Run one capture's argv and return its rendered artifact text."""
    subs = ctx.substitutions(cap)
    for entry in cap.mkdir:
        target = Path(_substitute(entry, subs)).resolve()
        if not target.is_relative_to(ctx.tmp.resolve()):
            raise CaptureError(f"capture {cap.id!r}: mkdir {entry!r} is outside the scratch dir")
        target.mkdir(parents=True, exist_ok=True)
    argv = [_substitute(a, subs) for a in cap.argv]
    with _appended_settings(cap, ctx):
        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv from a committed manifest, no shell
                argv,
                env=ctx.env(cap),
                cwd=str(ctx.tmp),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # one stream, so ordering survives
                text=True,
                timeout=cap.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # ``text=True`` isn't honoured on a timeout: CPython's subprocess.run
            # never applies the text-mode decode step to what it captured before
            # killing the process, so ``exc.output`` is raw bytes here even
            # though every non-timeout path already sees ``str``.
            raw = exc.output
            partial = raw.decode(errors="replace") if isinstance(raw, bytes) else (raw or "")
            raise CaptureError(
                f"capture {cap.id!r} timed out after {cap.timeout}s; output so far:\n{partial}"
            ) from exc
        except FileNotFoundError as exc:
            raise CaptureError(f"capture {cap.id!r}: command not found: {argv[0]}") from exc
    if proc.returncode != cap.expect_exit:
        raise CaptureError(
            f"capture {cap.id!r} exited {proc.returncode}, "
            f"expected {cap.expect_exit}:\n{proc.stdout}"
        )
    body = redact(proc.stdout, ctx.default_rules() + cap.redact)
    return render_command(cap.argv) + "\n" + body.rstrip("\n") + "\n"


def _substitute(arg: str, subs: dict[str, str]) -> str:
    for key, value in subs.items():
        arg = arg.replace(key, value)
    return arg


def refresh(caps: list[Capture], ctx: RunContext) -> None:
    """Run every capture in ``caps`` and (over)write its artifact."""
    if not caps:
        raise CaptureError("no captures selected")
    ctx.captures_dir.mkdir(parents=True, exist_ok=True)
    for cap in caps:
        (ctx.captures_dir / f"{cap.id}.txt").write_text(run_capture(cap, ctx))
        print(f"captured {cap.id}", flush=True)


def check(caps: list[Capture], ctx: RunContext) -> int:
    """Diff each capture's fresh output against its committed artifact; 1 if any drifted, else 0."""
    if not caps:
        raise CaptureError("no captures selected")
    drift = 0
    for cap in caps:
        target = ctx.captures_dir / f"{cap.id}.txt"
        fresh = run_capture(cap, ctx)
        if not target.exists():
            print(f"MISSING {target.relative_to(ROOT) if target.is_relative_to(ROOT) else target}")
            drift += 1
            continue
        committed = target.read_text()
        if committed != fresh:
            drift += 1
            print(f"DRIFT {cap.id}")
            sys.stdout.writelines(
                difflib.unified_diff(
                    committed.splitlines(keepends=True),
                    fresh.splitlines(keepends=True),
                    fromfile=f"committed/{cap.id}.txt",
                    tofile=f"fresh/{cap.id}.txt",
                )
            )
    print(f"{len(caps)} capture(s) checked, {drift} drifted", flush=True)
    return 1 if drift else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: parse args, select captures, then list/check/refresh."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check", action="store_true", help="diff against committed artifacts; exit 1 on drift"
    )
    parser.add_argument("--labless", action="store_true", help="only captures that need no bed")
    parser.add_argument(
        "--only", action="append", default=[], metavar="ID", help="capture id (repeatable)"
    )
    parser.add_argument("--list", action="store_true", help="list captures and exit")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--captures-dir", type=Path, default=DEFAULT_CAPTURES)
    args = parser.parse_args(argv)

    caps = select(load_manifest(args.manifest), labless=args.labless, only=args.only)
    if args.list:
        for cap in caps:
            print(f"{cap.id:32} {'labless' if cap.labless else 'bed':8} {render_command(cap.argv)}")
        return 0
    shutil.rmtree(SCRATCH_DIR, ignore_errors=True)
    SCRATCH_DIR.mkdir(parents=True)
    try:
        ctx = RunContext(examples_root=EXAMPLES, captures_dir=args.captures_dir, tmp=SCRATCH_DIR)
        if args.check:
            return check(caps, ctx)
        refresh(caps, ctx)
        return 0
    finally:
        shutil.rmtree(SCRATCH_DIR, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CaptureError as exc:
        print(f"docs captures: {exc}", file=sys.stderr)
        sys.exit(2)
