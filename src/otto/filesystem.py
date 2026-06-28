"""Network-filesystem detection.

A small, stdlib-only helper used to decide whether a path lives on a network
filesystem (NFS, CIFS/SMB, sshfs, …). otto uses this to adapt behaviour that is
unsafe or slow on shared storage — notably the monitor SQLite database, whose
WAL journal mode is unsupported over a network filesystem.

Linux-only (otto targets Linux): detection reads ``/proc/self/mountinfo``. Any
failure to read or parse it is treated as "local" — a safe default, since the
only consequence of misdetection is a (harmless-for-otto's workload)
journal-mode change.

This module imports nothing from ``otto`` so it can never create an import cycle.
"""

from pathlib import Path

_MOUNTINFO_PATH = "/proc/self/mountinfo"

# Filesystem types treated as "network/shared". Deliberately an explicit set —
# we do NOT blanket-flag all ``fuse.*`` because local FUSE mounts are common.
_NETWORK_FSTYPES = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smb3",
        "smbfs",
        "fuse.sshfs",
        "glusterfs",
        "fuse.glusterfs",
        "lustre",
        "ceph",
        "fuse.ceph",
        "afs",
        "9p",
        "beegfs",
        "ocfs2",
        "gpfs",
    }
)


def _unescape_mountinfo(field: str) -> str:
    """Decode the octal escapes (``\\040`` space, ``\\011`` tab, …) mountinfo uses."""
    if "\\" not in field:
        return field
    out: list[str] = []
    i = 0
    n = len(field)
    while i < n:
        if field[i] == "\\" and i + 4 <= n and all(c in "01234567" for c in field[i + 1 : i + 4]):
            out.append(chr(int(field[i + 1 : i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def _parse_mountinfo(text: str) -> list[tuple[str, str]]:
    """Return ``(mountpoint, fstype)`` pairs parsed from mountinfo ``text``.

    mountinfo line layout::

        ID PARENT MAJ:MIN ROOT MOUNTPOINT OPTIONS... - FSTYPE SOURCE SUPEROPTS

    The mount point is field index 4; the fstype is the first field after the
    `` - `` separator (there are zero or more optional fields before it).
    """
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(" - ", 1)
        if len(parts) != 2:  # noqa: PLR2004 — mountinfo " - " separator always produces exactly 2 parts
            continue
        left_fields = parts[0].split()
        right_fields = parts[1].split()
        if len(left_fields) < 5 or not right_fields:  # noqa: PLR2004 — mountinfo has 5 required fields before the " - " separator (ID PARENT MAJ:MIN ROOT MOUNTPOINT)
            continue
        pairs.append((_unescape_mountinfo(left_fields[4]), right_fields[0]))
    return pairs


def _fstype_for_path(path_str: str, pairs: list[tuple[str, str]]) -> str | None:
    """Fstype of the longest mount-point prefix of ``path_str`` (or ``None``)."""
    best_len = -1
    best_fstype: str | None = None
    for mountpoint, fstype in pairs:
        if mountpoint == "/":
            is_under = True
            mp_len = 1
        else:
            mp = mountpoint.rstrip("/")
            is_under = path_str == mp or path_str.startswith(mp + "/")
            mp_len = len(mp)
        if is_under and mp_len > best_len:
            best_len = mp_len
            best_fstype = fstype
    return best_fstype


def _read_mountinfo() -> str | None:
    try:
        with open(_MOUNTINFO_PATH, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _resolve_existing(path: str | Path) -> str:
    """Resolve ``path`` absolutely, walking up to the nearest existing parent.

    The target (e.g. the DB file) may not exist yet at detection time; we still
    want the mount point of the directory it will live in.
    """
    p = Path(path).resolve()
    while not p.exists() and p != p.parent:
        p = p.parent
    return str(p)


def network_fs_type(path: str | Path) -> str | None:
    """Return the network filesystem type backing ``path``, or ``None``.

    Returns the mountinfo fstype string (e.g. ``"nfs4"``, ``"cifs"``,
    ``"fuse.sshfs"``) when ``path`` is on a network filesystem; otherwise
    ``None``. Detection failures are treated as local (return ``None``).
    """
    text = _read_mountinfo()
    if text is None:
        return None
    fstype = _fstype_for_path(_resolve_existing(path), _parse_mountinfo(text))
    return fstype if fstype in _NETWORK_FSTYPES else None


def is_network_fs(path: str | Path) -> bool:
    """``True`` when ``path`` lives on a network/shared filesystem."""
    return network_fs_type(path) is not None
