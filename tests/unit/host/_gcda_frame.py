"""The counter reset check's marker frame, as host doubles must answer it.

The check script prints ``printf '\\n%s\\n' otto-gcda-check`` before ``find``
(``otto.host.product.gcda_check_cmd``), and its parser reads only what
follows the marker. A double that answers the check must print the frame
too, exactly as a shell would. Restated here rather than imported from the
product, so a change to the script turns these doubles RED.

The reset's removal line (``otto.host.product.gcda_delete_cmd``) and its
guarded check line (``gcda_check_cmd``) are restated the same way, as
:func:`delete_line` and :func:`check_line`.
"""

import shlex

CHECK_MARK = "otto-gcda-check"

CHECK_PREFIX = f"printf '\\n%s\\n' {CHECK_MARK}; "
"""The check command's opening, as the pins spell it."""

CHECK_FRAME = f"\n{CHECK_MARK}\n"
"""What that opening prints."""


def framed(cmd: str, output: str) -> str:
    """*output* as a shell returns it for *cmd*: the check's frame first."""
    return CHECK_FRAME + output if CHECK_MARK in cmd else output


def absent_test(cov_dir: str) -> str:
    """The guard the removal and the check share: *cov_dir* provably absent."""
    q = shlex.quote(cov_dir)
    return (
        f'{{ __otto_d={q}; while ! test -e "$__otto_d" && ! test -L "$__otto_d"; '
        "do __otto_d=${__otto_d%/*}; __otto_d=${__otto_d:-/}; done; "
        f'test "$__otto_d" != {q} && {{ ! test -d "$__otto_d" || test -x "$__otto_d"; }}; }}'
    )


def check_line(cov_dir: str) -> str:
    """The reset's guarded check for *cov_dir*, as the pins spell it."""
    q = shlex.quote(cov_dir)
    return (
        f"{CHECK_PREFIX}if {absent_test(cov_dir)}; then echo otto-gcda-absent; "
        f"else LC_ALL=C find {q} -name '*.gcda' -type f -print 2>&1; fi"
    )


def delete_line(cov_dir: str, *, keep_cov_dir: bool = False) -> str:
    """The reset's guarded removal line for *cov_dir*, as the pins spell it.

    The emptied *cov_dir* itself is removed last, unless *keep_cov_dir* (a
    kind whose ``cov_dir`` is a live container's bind-mount source).
    """
    q = shlex.quote(cov_dir)
    find = f"find {q} -name '*.gcda' -type f -exec rm -f {{}} \\; ! -path '*\n*' -print"
    ancestors = (
        "while IFS= read -r f; do d=${f%/*}; "
        f"while case $d in {q}/*) true;; *) false;; esac; "
        "do printf '%s\\n' \"$d\"; d=${d%/*}; done; done"
    )
    prune = 'while IFS= read -r d; do rmdir "$d" 2>/dev/null; done'
    root = "" if keep_cov_dir else f"rmdir {q} 2>/dev/null; "
    return (
        f"{absent_test(cov_dir)} || "
        f'{{ __otto_gcda_rc=$( {{ {{ {find}; echo "$?" >&3; }} | {ancestors} | '
        "{ LC_ALL=C sort -ru 2>/dev/null || while IFS= read -r _; do :; done; } | "
        f'{prune}; }} 3>&1 ); {root}test "$__otto_gcda_rc" = 0; }}'
    )
