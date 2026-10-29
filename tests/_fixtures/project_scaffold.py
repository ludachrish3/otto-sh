"""THE project scaffold for suite code (review §7.5).

Every ``.otto/settings.toml`` the suite writes must come from here — the
drift guard in ``tests/unit/test_project_scaffold_policy.py`` enforces it
(writer-under-test sites carry a ``# scaffold-exempt: <reason>`` marker
instead).  Before this module existed the mkdir → write-literal → ``Project``
dance was re-typed at 46 sites across 28 files (by this wave's landed
guard; its first cut counted 43/25 before the local-binding arm existed),
so every new settings field meant two dozen edits and the copies drifted.

``extra`` is VERBATIM TOML appended after the generated header — the
dependency tables, coverage sections, and host preferences the sites write
today are carried byte-for-byte (preserve the computation; a renderer would
re-encode them).  Callers construct ``Project(root=...)`` themselves: this
module deliberately imports nothing from otto, so fixture import order can
never entangle with product import-time behavior.
"""

import shutil
from pathlib import Path


def make_project(
    root: Path,
    *,
    name: str = "sut",
    version: str = "1.0.0",
    tests: list[str] | None = None,
    extra: str = "",
    files: dict[str, str] | None = None,
    known_hosts: Path | None = None,
) -> Path:
    """Create *root* as an otto project and return it.

    Writes ``root/.otto/settings.toml`` with ``name``/``version`` lines, a
    ``tests = [...]`` line when *tests* is given, then *extra* verbatim
    (separated by a blank line).  *files* are written under *root* (parents
    created) — test suites, conftests, source files.

    *known_hosts* is for a project whose otto subprocess dials a host over
    SSH: it adds ``[host_preferences.".*".ssh_options] known_hosts = ...``,
    so the keys that run records land in that file rather than the real
    ``~/.ssh/known_hosts`` (which the root conftest's ``_hermetic_known_hosts``
    keeps out of in-process tests, but cannot reach in a subprocess).
    """
    for field_name, value in (("name", name), ("version", version)):
        if '"' in value or "\\" in value:
            raise ValueError(
                f"{field_name}={value!r} needs TOML escaping the plain renderer "
                "does not do — pass simple values or write the file via `extra`"
            )
    otto_dir = root / ".otto"
    otto_dir.mkdir(parents=True)
    header = f'name = "{name}"\nversion = "{version}"\n'
    if tests is not None:
        rendered = ", ".join(f'"{t}"' for t in tests)
        header += f"tests = [{rendered}]\n"
    if known_hosts is not None:
        extra = (extra.rstrip("\n") + "\n\n" if extra else "") + (
            f'[host_preferences.".*".ssh_options]\nknown_hosts = "{known_hosts}"\n'
        )
    body = header + ("\n" + extra.rstrip("\n") + "\n" if extra else "")
    (otto_dir / "settings.toml").write_text(body)
    root_resolved = root.resolve()
    for rel, content in (files or {}).items():
        p = root / rel
        if not p.resolve().is_relative_to(root_resolved):
            raise ValueError(f"files key {rel!r} escapes the project root")
        if p.resolve() == (otto_dir / "settings.toml").resolve():
            raise ValueError("files= must not overwrite the settings the scaffold just wrote")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


def touch_settings(root: Path) -> Path:
    """An EMPTY ``.otto/settings.toml`` under *root*, for project STAND-INS.

    The completion-cache tests hash the settings file as a fingerprint source
    on ``MagicMock``/``SimpleNamespace`` projects that are never parsed by
    ``Project`` — they need a file to hash, not a project.  Centralized so the
    ``.otto/settings.toml`` layout is spelled in exactly one module.
    """
    otto_dir = root / ".otto"
    otto_dir.mkdir(parents=True, exist_ok=True)
    settings = otto_dir / "settings.toml"
    settings.write_text("")
    return settings


_INSTALL_ARTIFACTS = ("*.egg-info", "*.dist-info", "__pycache__", "*.pyc")
"""What an editable install and a run of the interpreter leave behind."""


def copy_sample_project(src: Path, dst: Path) -> Path:
    """Copy the checked-in sample project at *src* to *dst* and return *dst*.

    FOR ANY TEST THAT INSTALLS A SAMPLE REPO.  ``pip install -e`` /
    ``uv pip install -e`` writes an ``.egg-info`` into the directory it
    installs, so pointing one at ``tests/project4`` mutates the suite's own
    corpus — and since otto puts each project's declared ``libs`` on ``sys.path``,
    that directory then reads as an INSTALLED DISTRIBUTION to
    ``importlib.metadata``.  Anything asking metadata a question about the project
    (the dependency preflight does) then answers differently on a machine that
    has run the install suite than on a clean clone, and ``.egg-info`` is
    gitignored so ``git status`` shows nothing.
    ``tests/unit/test_sample_projects_hygiene.py`` fails when it happens.

    Artifacts are stripped on the way through: a copy that carried the previous
    run's ``.egg-info`` would reproduce the bug inside ``tmp_path``, which is
    harder to see, not easier.

    ``copytree`` copies via ``copy2``, so MTIMES ARE PRESERVED — the env
    staleness comparison reads the copy's ``pyproject.toml`` mtime and gets the
    same answer it would from the original.
    """
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*_INSTALL_ARTIFACTS))
    return dst
