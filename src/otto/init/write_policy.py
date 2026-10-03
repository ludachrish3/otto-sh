"""The one write policy every file ``otto init`` scaffolds goes through.

Otto-owned files (the editor schemas, the VS Code snippets, the exported
kmodcov library) are generated from the installed otto and refreshed on every
write. User-owned files (settings, lab data, example tests, the init module,
the VS Code settings, the kmodcov consumer starter) are created only when
absent and never edited afterwards: once written, they are the user's.
"""

import dataclasses
import os
from pathlib import Path
from typing import Literal

Owner = Literal["otto", "user"]
Outcome = Literal["created", "refreshed", "kept", "pruned"]


@dataclasses.dataclass(frozen=True)
class FileWrite:
    """One file the scaffolder touched, and what it did to it.

    ``pruned`` is a schema the installed otto no longer emits, deleted from
    the otto-owned ``.otto/schemas``.
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
