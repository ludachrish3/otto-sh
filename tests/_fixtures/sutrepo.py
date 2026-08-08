"""THE SUT-repo scaffold for suite code (review §7.5).

Every ``.otto/settings.toml`` the suite writes must come from here — the
drift guard in ``tests/unit/test_sutrepo_scaffold_policy.py`` enforces it
(writer-under-test sites carry a ``# sutrepo-exempt: <reason>`` marker
instead).  Before this module existed the mkdir → write-literal → ``Repo``
dance was re-typed at 46 sites across 28 files (by this wave's landed
guard; its first cut counted 43/25 before the local-binding arm existed),
so every new settings field meant two dozen edits and the copies drifted.

``extra`` is VERBATIM TOML appended after the generated header — the
dependency tables, coverage sections, and host preferences the sites write
today are carried byte-for-byte (preserve the computation; a renderer would
re-encode them).  Callers construct ``Repo(sut_dir=...)`` themselves: this
module deliberately imports nothing from otto, so fixture import order can
never entangle with product import-time behavior.
"""

from pathlib import Path


def make_sut_repo(
    root: Path,
    *,
    name: str = "sut",
    version: str = "1.0.0",
    tests: list[str] | None = None,
    extra: str = "",
    files: dict[str, str] | None = None,
) -> Path:
    """Create *root* as an otto SUT repo and return it.

    Writes ``root/.otto/settings.toml`` with ``name``/``version`` lines, a
    ``tests = [...]`` line when *tests* is given, then *extra* verbatim
    (separated by a blank line).  *files* are written under *root* (parents
    created) — test suites, conftests, source files.
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
    body = header + ("\n" + extra.rstrip("\n") + "\n" if extra else "")
    (otto_dir / "settings.toml").write_text(body)
    root_resolved = root.resolve()
    for rel, content in (files or {}).items():
        p = root / rel
        if not p.resolve().is_relative_to(root_resolved):
            raise ValueError(f"files key {rel!r} escapes the repo root")
        if p.resolve() == (otto_dir / "settings.toml").resolve():
            raise ValueError("files= must not overwrite the settings the scaffold just wrote")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


def touch_settings(sut_dir: Path) -> Path:
    """An EMPTY ``.otto/settings.toml`` under *sut_dir*, for repo STAND-INS.

    The completion-cache tests hash the settings file as a fingerprint source
    on ``MagicMock``/``SimpleNamespace`` repos that are never parsed by
    ``Repo`` — they need a file to hash, not a repo.  Centralized so the
    ``.otto/settings.toml`` layout is spelled in exactly one module.
    """
    otto_dir = sut_dir / ".otto"
    otto_dir.mkdir(parents=True, exist_ok=True)
    settings = otto_dir / "settings.toml"
    settings.write_text("")
    return settings
