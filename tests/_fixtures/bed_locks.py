"""Where every bed lock lives: one directory for every session of this user.

A host lease (``_host_pool``) or a console lock (``_console_lock``) only
excludes another holder that opens the same file. The directory used to be
``tmp_path_factory.getbasetemp().parent``, which pytest makes per session
under xdist (a worker's basetemp is ``<session>/<gw>``), per user under
``-n0`` and anything under ``--basetemp``: two sessions on one lab host locked
two files. This root depends on none of that. It is a fixed ``/tmp`` path,
never ``TMPDIR``, so a session whose temp dir differs still meets the others
here, the way ``/tmp/otto-gs.lock`` does for the docs captures.
``BED_LOCK_ROOT`` moves it, for tests that must not touch the real locks.
"""

import getpass
import os
from pathlib import Path

BED_LOCK_ROOT_ENV = "BED_LOCK_ROOT"


def bed_lock_root() -> Path:
    """Return the bed-lock directory, made on first use."""
    # A literal /tmp, not tempfile.gettempdir(): every session of this user meets here.
    default = f"/tmp/otto-bed-locks-{getpass.getuser()}"
    root = Path(os.environ.get(BED_LOCK_ROOT_ENV) or default)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def lock_dir_or_root(lock_dir: Path | None) -> Path:
    """Return ``lock_dir`` when a test of the lock injects one, else :func:`bed_lock_root`."""
    return bed_lock_root() if lock_dir is None else lock_dir
