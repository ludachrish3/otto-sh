"""The one write policy every file ``otto init`` scaffolds goes through.

Otto-owned files (the editor schemas, the VS Code snippets, the exported
kmodcov library, the embedded-gcov patch) are generated from the installed
otto and refreshed on every write. User-owned files (settings, lab data,
example tests, the init module, the VS Code settings, ``.gitignore`` and
``.dockerignore``, the kmodcov and embedded-gcov consumer starters) are created
only when absent and never edited afterwards: once written, they are the user's.

The one exception is :func:`append_block`: a block added at the END of an
existing user file. In ``settings.toml`` it is a block of TOML comments (the
kmodcov area's commented ``[[dev_tools]]`` entry, the embedded-gcov area's
commented ``[coverage.embedded]`` block), which changes nothing the file
means; in ``.gitignore`` and ``.dockerignore`` it is the one line that ignores
otto's run output. Nothing already in the file is changed.
"""

import dataclasses
import os
from pathlib import Path
from typing import Literal

Owner = Literal["otto", "user"]
Outcome = Literal["created", "refreshed", "kept", "pruned", "appended"]


@dataclasses.dataclass(frozen=True)
class FileWrite:
    """One file the scaffolder touched, and what it did to it.

    ``pruned`` is a schema the installed otto no longer emits, deleted from
    the otto-owned ``.otto/schemas``. ``appended`` is a user-owned file that
    gained a block at its end (:func:`append_block`).
    """

    path: Path
    outcome: Outcome


def write_file(path: Path, text: str, owner: Owner, *, mode: int | None = None) -> FileWrite:
    """Write *text* to *path* (UTF-8, whatever the locale) under *owner*'s policy.

    *mode* (e.g. ``0o600``) is set on a file this call creates, and re-applied
    when an otto-owned file is refreshed.

    A user-owned *path* that exists — including any symlink, dangling or
    not — is ``kept`` and never followed: writing through a link would edit
    a file the user owns somewhere else.
    """
    existed = path.exists() or path.is_symlink()
    if existed and owner == "user":
        return FileWrite(path, "kept")
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None or existed:
        path.write_text(text, encoding="utf-8")
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    if mode is not None:
        path.chmod(mode)  # umask cannot widen 0o600, but be explicit
    return FileWrite(path, "refreshed" if existed else "created")


def append_block(path: Path, block: str) -> FileWrite:
    """Add *block* to the end of the existing file *path*, keeping every byte already there.

    A file that does not end in a newline gets one first, so the block starts
    on its own line. The caller decides whether the block is already present;
    this only appends. A symlink is ``kept`` and never followed, as
    :func:`write_file` keeps it.
    """
    if path.is_symlink():
        return FileWrite(path, "kept")
    text = path.read_bytes().decode("utf-8", errors="replace")
    lead = "" if not text or text.endswith("\n") else "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(lead + block)
    return FileWrite(path, "appended")
