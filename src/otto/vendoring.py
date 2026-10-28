"""Copy a package's shipped files into a user's directory, and compare a copy with them.

Two verbs hand a user sources that ship inside the wheel: ``otto cov kmodcov``
and ``otto cov embedded-gcov``. Both write the files verbatim, report only what
they changed, and compare byte for byte; that decision lives here once.
"""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FileComparison:
    """How a directory's copy of the shipped *names* stands against their source."""

    missing: list[str] = field(default_factory=list)
    differing: list[str] = field(default_factory=list)
    equal: list[str] = field(default_factory=list)

    @property
    def matches(self) -> bool:
        """Report whether every name is present and identical."""
        return not self.missing and not self.differing


def export_files(source_dir: Path, names: list[str], dest: Path) -> list[str]:
    """Write *names* from *source_dir* into *dest*, overwriting; return the names that changed.

    A name changed when its content differs from what was there or its execute
    bits do. Only the EXECUTE bits travel with the copy, because they are the
    only mode bits version control tracks: an installed wheel's 0644 against a
    umask-002 checkout's 0664 is not a difference, and reporting it would name
    every file as written over a clean tree.
    """
    dest.mkdir(parents=True, exist_ok=True)
    changed = []
    for name in names:
        source = source_dir / name
        data = source.read_bytes()
        x_bits = source.stat().st_mode & 0o111
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        content_changed = not target.is_file() or target.read_bytes() != data
        if content_changed:
            target.write_bytes(data)
        mode = target.stat().st_mode & 0o777
        if content_changed or (mode & 0o111) != x_bits:
            target.chmod((mode & ~0o111) | x_bits)
            changed.append(name)
    return changed


def compare_files(source_dir: Path, names: list[str], directory: Path) -> FileComparison:
    """Sort *names* into missing from, differing in and equal in *directory*, byte for byte."""
    found = FileComparison()
    for name in names:
        target = directory / name
        if not target.is_file():
            found.missing.append(name)
        elif target.read_bytes() != (source_dir / name).read_bytes():
            found.differing.append(name)
        else:
            found.equal.append(name)
    return found


@dataclass
class CheckResult:
    """What a ``check`` found; ``state`` is ``current``, ``differs`` or ``absent``."""

    directory: Path
    state: str
    differing: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """Map :attr:`state` to the exit status every ``check`` verb leaves behind."""
        return {"current": 0, "differs": 1, "absent": 2}[self.state]

    @property
    def differences(self) -> str:
        """Render the ``differs: …; missing: …`` clause every ``check`` verb prints."""
        parts = []
        if self.differing:
            parts.append(f"differs: {', '.join(self.differing)}")
        if self.missing:
            parts.append(f"missing: {', '.join(self.missing)}")
        return "; ".join(parts)


def check_result(directory: Path, found: FileComparison, *, absent: bool) -> CheckResult:
    """Fold a :class:`FileComparison` into a result; *absent* is the caller's own rule.

    Each package decides what "nothing of ours is there" means (the kmodcov
    library keys on one header, the embedded-gcov patch on every file missing);
    everything else is ``current`` when :attr:`FileComparison.matches`, else
    ``differs``.
    """
    if absent:
        return CheckResult(directory, "absent", missing=found.missing)
    state = "current" if found.matches else "differs"
    return CheckResult(directory, state, found.differing, found.missing)
