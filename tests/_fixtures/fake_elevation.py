"""Stand-in ``sudo`` and ``su`` scripts, for tests that type an elevation into a real local shell.

One stand-in of each, shared: the A2 measurement driver's dry run
(``tests/measurements/a2_exec_frame.py``) and the host unit tests'
``fake_sudo_on_path`` / ``fake_su_on_path`` fixtures
(``tests/unit/host/conftest.py``) both install them with
:func:`install_fake_elevation`.

``sudo`` reads ``SUDO_PROMPT``, takes ``-k``, ``-V``, ``-S``, ``-n``, ``-v``
and ``-l``, prompts on stderr and reads the password from fd 0, three tries
(``passwd_tries``; one prompt and status 1 at ``1``, as sudoers' ``passwd_tries=1``),
and keeps a timestamp unless told not to (``no-timestamp`` in its state
directory). ``-n`` runs only with a timestamp. ``-l CMD`` answers whether CMD may run:
status 0 and its name, or status 1. With ``refuse-sh`` in its state
directory the policy allows every command but a shell (``sh``, ``bash``,
``dash``, ``ash``, ``ksh``, ``mksh``, ``zsh``, ``busybox``, by base name), as a
sudoers that lists commands does: ``sudo <shell>`` and ``sudo -l <shell>``
are refused after authenticating, as real sudo refuses them. With
``nopasswd`` it never asks (a ``NOPASSWD`` rule). Every password line it
reads is counted, one line per read, in ``reads``. A command runs with
``FAKE_ROOT=1``, which the stand-in ``id`` reads. ``su -c CMD`` prompts
``Password:`` on stderr, reads root's password from fd 0 once, and runs
``sh -c CMD``. State lives in ``$FAKE_SUDO_DIR``: ``password``, ``listing``,
``ts``, ``per-tty`` (the timestamp is kept per controlling terminal, as
sudo's default ``timestamp_type=tty``), ``tries``, ``no-timestamp``, ``refuse-sh``,
``nopasswd`` and ``reads``.
"""

import os
import shlex
import stat
from pathlib import Path

FAKE_SUDO = r"""#!/bin/sh
# A stand-in for sudo. State lives in $FAKE_SUDO_DIR:
# `password`, `listing`, `no-timestamp` (present: a validation is not kept)
# and `refuse-sh` (present: every command but `sh` is allowed).
ts="$FAKE_SUDO_DIR/ts"
[ -e "$FAKE_SUDO_DIR/per-tty" ] && ts="$FAKE_SUDO_DIR/ts-$(ps -o tty= -p $$ | tr -d ' /')"
pw=$(cat "$FAKE_SUDO_DIR/password"); n=0; mode=
while [ $# -gt 0 ]; do
  case $1 in
    -k) rm -f "$ts"; exit 0;;
    -V) echo "Sudo version 1.9.0fake"; exit 0;;
    -S) shift;;
    -n) n=1; shift;;
    -v) mode=v; shift;;
    -l) mode=l; shift;;
    *) break;;
  esac
done
auth() {
  [ -e "$ts" ] && return 0
  [ -e "$FAKE_SUDO_DIR/nopasswd" ] && return 0
  [ "$n" = 1 ] && { echo "sudo: a password is required" >&2; return 1; }
  tries=0; most=$(cat "$FAKE_SUDO_DIR/tries")
  while [ "$tries" -lt "$most" ]; do
    printf %s "${SUDO_PROMPT-[sudo] password: }" >&2
    IFS= read -r got || return 1
    echo read >>"$FAKE_SUDO_DIR/reads"
    [ -z "$got" ] && { echo "sudo: no password was provided" >&2; return 1; }
    if [ "$got" = "$pw" ]; then
      [ -e "$FAKE_SUDO_DIR/no-timestamp" ] || : >"$ts"
      return 0
    fi
    echo "Sorry, try again." >&2; tries=$((tries+1))
  done
  return 1
}
auth || exit 1
allowed() {
  [ -e "$FAKE_SUDO_DIR/refuse-sh" ] || return 0
  case ${1##*/} in sh|bash|dash|ash|ksh|mksh|zsh|busybox) return 1;; esac
}
case $mode in
  v) exit 0;;
  l) [ $# -eq 0 ] && { cat "$FAKE_SUDO_DIR/listing"; exit 0; }
     allowed "$1" || exit 1; echo "$1"; exit 0;;
esac
allowed "$1" || {
  echo "Sorry, user $(id -un) is not allowed to execute '$1' as root." >&2; exit 1; }
FAKE_ROOT=1 exec "$@"
"""

FAKE_SU = r"""#!/bin/sh
# A stand-in for su -c: root's password ($FAKE_SUDO_DIR/password) from fd 0, once.
[ "${1-}" = -c ] || { echo "su: only -c CMD is modelled" >&2; exit 1; }
printf 'Password: ' >&2
IFS= read -r got || exit 1
[ "$got" = "$(cat "$FAKE_SUDO_DIR/password")" ] || {
  echo "su: Authentication failure" >&2; exit 1; }
FAKE_ROOT=1 exec sh -c "$2"
"""

FAKE_ID = r"""#!/bin/sh
# `id` as the fake sudo's command sees it: root under FAKE_ROOT, the real one otherwise.
if [ "${FAKE_ROOT-}" = 1 ] && [ "${1-}" = -un ]; then echo root; exit 0; fi
exec /usr/bin/id "$@"
"""

FAKE_LISTING = """Matching Defaults entries for dry on this host:
    env_reset, timestamp_timeout={timeout}, timestamp_type=tty

User dry may run the following commands on this host:
    (ALL : ALL) ALL
"""


def install_fake_elevation(
    root: Path,
    *,
    password: str,
    keeps_timestamp: bool,
    refuses_shell: bool = False,
    nopasswd: bool = False,
    passwd_tries: int = 3,
    per_tty: bool = False,
) -> "dict[str, str]":
    """Put the stand-in ``sudo``, ``su`` and ``id`` under *root*; return the variables using them.

    The answer is ``PATH`` (``<root>/bin`` first) and ``FAKE_SUDO_DIR``; each
    script also defaults ``FAKE_SUDO_DIR`` to *root*'s state, so it works
    from an emptied environment too.
    Installing again over the same *root* rewrites the state and forgets
    any timestamp and every counted read. *refuses_shell* allows every
    command but a shell; *nopasswd* never asks; *passwd_tries* is how many
    prompts a wrong password gets before sudo exits 1; *per_tty* keeps the
    timestamp per controlling terminal.
    """
    bin_dir, state = root / "bin", root / "sudo"
    bin_dir.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    # The state directory is also written into each script, so a stand-in
    # started with an emptied environment (a console exec's `env -i`) finds
    # it as a real sudo finds its own state.
    default = f": ${{FAKE_SUDO_DIR:={shlex.quote(str(state))}}}\n"
    for name, text in (("sudo", FAKE_SUDO), ("su", FAKE_SU), ("id", FAKE_ID)):
        path = bin_dir / name
        shebang, rest = text.split("\n", 1)
        path.write_text(f"{shebang}\n{default}{rest}")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    (state / "password").write_text(password + "\n")
    (state / "tries").write_text(f"{passwd_tries}\n")
    (state / "listing").write_text(FAKE_LISTING.format(timeout=15 if keeps_timestamp else 0))
    marker = state / "no-timestamp"
    if keeps_timestamp:
        marker.unlink(missing_ok=True)
    else:
        marker.touch()
    for name, present in (
        ("refuse-sh", refuses_shell),
        ("nopasswd", nopasswd),
        ("per-tty", per_tty),
    ):
        if present:
            (state / name).touch()
        else:
            (state / name).unlink(missing_ok=True)
    (state / "reads").unlink(missing_ok=True)
    for stamp in state.glob("ts*"):
        stamp.unlink()
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_SUDO_DIR": str(state)}
