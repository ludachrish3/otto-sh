#!/usr/bin/env python3
"""Generate the docs' termynal terminal blocks from the real CLI at build time.

Like the GUI media (``capture_docs_media.py``), everything under
``docs/_static/generated/termynal/`` is a product of the build, never
committed: docs/conf.py runs this script on every Sphinx build, so the help
text and completion candidates shown in the docs are captured from the
*current* CLI and can never drift from it.

How it works: ``otto init --all`` scaffolds a small demo repo into a temp
directory (the same scaffold a new user gets), and every capture runs against
it — ``otto <command> --help`` for the per-command pages, and typer's
completion protocol (``_OTTO_COMPLETE=complete_bash`` + ``COMP_WORDS``) for
the tab-completion showcases, so the candidates come from otto's real
registries and completion cache, not hand-written examples. Output is one
HTML snippet per capture, included by the docs via ``{raw} html :file:``
and animated client-side by the vendored ``docs/_static/termynal.js``.

Modes — ``--mode`` flag, or the ``OTTO_DOCS_MEDIA`` env var (shared with the
GUI media pipeline): ``auto`` regenerates only when the stamp says the inputs
changed; ``force`` always regenerates; ``placeholder`` writes minimal static
blocks without running the CLI.
"""

import argparse
import hashlib
import html
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "docs" / "_static" / "generated" / "termynal"
STAMP = OUT_DIR / ".stamp"

# Help text and completion candidates can come from anywhere in the package
# (verb docstrings live on host classes, help strings on CLI modules, the
# scaffold templates in cli/init.py), so the stamp covers all of src/otto.
_STAMP_INPUTS = [Path(__file__).resolve(), REPO_ROOT / "src" / "otto"]

# The nine first-party commands; each lifecycle page embeds its --help.
COMMANDS = ["run", "test", "host", "monitor", "cov", "docker", "reservation", "schema", "init"]

# Tab-completion showcases: snippet name -> the COMP_WORDS line completed.
# Every candidate list is served by otto's real completion machinery — the
# registries (suites, instructions, term backends), the class-scoped host
# verb menu, and the host-id sources.
COMPLETIONS = {
    "host-ids": "otto host ",
    "host-verbs": "otto host example-device ",
    "term-backends": "otto host example-device --term ",
    "instructions": "otto run ",
    "suites": "otto test ",
    "lab-names": "otto --lab ",
    "test-names": "otto test --tests ",
}

_PLACEHOLDER_LINE = "(placeholder — build once with Chromium/CLI available for real output)"


def _artifact_names() -> list[str]:
    helps = ["help-otto.html"] + [f"help-{c}.html" for c in COMMANDS]
    completes = [f"complete-{name}.html" for name in COMPLETIONS]
    return helps + completes


def _input_digest() -> str:
    h = hashlib.sha256()
    for root in _STAMP_INPUTS:
        files = sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else [root]
        for f in files:
            if "__pycache__" in f.parts:
                continue
            h.update(str(f.relative_to(REPO_ROOT)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def _is_fresh(digest: str) -> bool:
    if not all((OUT_DIR / name).exists() for name in _artifact_names()):
        return False
    return STAMP.exists() and STAMP.read_text().strip() == digest


def _snippet(input_line: str, output_lines: list[str]) -> str:
    """One termynal container: a typed input line, then the captured output."""
    spans = [f'  <span data-ty="input">{html.escape(input_line)}</span>']
    for line in output_lines:
        text = html.escape(line.rstrip())
        spans.append(f"  <span data-ty>{text}</span>" if text else "  <span data-ty></span>")
    body = "\n".join(spans)
    return f'<div data-termynal data-ty-typeDelay="25" data-ty-lineDelay="35">\n{body}\n</div>\n'


def _write_placeholders() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name in _artifact_names():
        (OUT_DIR / name).write_text(_snippet("otto", [_PLACEHOLDER_LINE]))
    STAMP.unlink(missing_ok=True)  # placeholders are never "fresh"
    print("docs termynal: wrote PLACEHOLDERS (CLI not run) — blocks are degraded", flush=True)


def _otto_env(demo: Path) -> dict[str, str]:
    """Build a clean environment rooted at the demo repo, with deterministic width."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTTO_")}
    env.update(
        OTTO_SUT_DIRS=str(demo),
        NO_COLOR="1",
        TERM="dumb",
        COLUMNS="80",
        LINES="50",
    )
    return env


def _run_otto(args: list[str], demo: Path, *, no_repo: bool = False, **env_extra: str) -> str:
    otto_bin = Path(sys.executable).parent / "otto"  # console script; runs entry()
    env = _otto_env(demo)
    if no_repo:  # `otto init` runs before the demo repo exists to be discovered
        env.pop("OTTO_SUT_DIRS", None)
    env.update(env_extra)
    proc = subprocess.run(  # noqa: S603 — venv otto binary + fixed argv, no shell
        [str(otto_bin), *args],
        env=env,
        cwd=demo,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"docs termynal: `otto {' '.join(args)}` exited {proc.returncode}:\n{proc.stderr}"
        )
    return proc.stdout


def _complete(words: str, demo: Path) -> list[str]:
    """Candidates for pressing TAB after *words*, via typer's completion protocol."""
    otto_bin = Path(sys.executable).parent / "otto"
    env = _otto_env(demo)
    env.update(
        _OTTO_COMPLETE="complete_bash",
        COMP_WORDS=words,
        COMP_CWORD=str(len(words.split()) + (1 if words.endswith(" ") else 0)),
    )
    proc = subprocess.run(  # noqa: S603 — venv otto binary, fixed argv, no shell
        [str(otto_bin)], env=env, cwd=demo, capture_output=True, text=True, timeout=120, check=False
    )
    candidates = [line for line in proc.stdout.splitlines() if line.strip()]
    if not candidates:
        raise SystemExit(
            f"docs termynal: completion for {words!r} returned no candidates — "
            "did the demo scaffold or the completion machinery change?"
        )
    return candidates


def _capture(demo: Path) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    help_targets = {"help-otto.html": []} | {f"help-{c}.html": [c] for c in COMMANDS}
    for name, cmd in help_targets.items():
        out = _run_otto([*cmd, "--help"], demo)
        input_line = " ".join(["otto", *cmd, "--help"])
        (OUT_DIR / name).write_text(_snippet(input_line, out.strip("\n").splitlines()))

    for name, words in COMPLETIONS.items():
        candidates = sorted(_complete(words, demo))
        # Render the way bash presents it: the prompt with TAB-TAB, the
        # candidate columns (wrapped at the 80-col terminal width), then the
        # prompt again awaiting more input.
        shown = textwrap.wrap("  ".join(candidates), width=78) or [""]
        (OUT_DIR / f"complete-{name}.html").write_text(
            _snippet(f"{words}<TAB><TAB>", [*shown, "", f"$ {words}"])
        )


def main() -> None:
    """Resolve the mode, scaffold the demo repo, and capture (or skip)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=["auto", "force", "placeholder"],
        default=os.environ.get("OTTO_DOCS_MEDIA", "auto"),
        help="auto: regenerate when stale; force: always; placeholder: no CLI run",
    )
    mode = parser.parse_args().mode

    if mode == "placeholder":
        _write_placeholders()
        return

    digest = _input_digest()
    if mode == "auto" and _is_fresh(digest):
        print("docs termynal: up to date (stamp matches) — skipping capture", flush=True)
        return

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="otto-docs-demo-") as tmp:
        demo = Path(tmp) / "acme"
        demo.mkdir()
        _run_otto(["init", "--all", "--path", str(demo), "--name", "acme"], demo, no_repo=True)
        _capture(demo)
    STAMP.write_text(digest + "\n")
    count = len(_artifact_names())
    print(
        f"docs termynal: captured {count} terminal blocks in {time.monotonic() - started:.1f}s",
        flush=True,
    )


if __name__ == "__main__":
    main()
