"""The counter reset check's marker frame, as host doubles must answer it.

The check script prints ``printf '\\n%s\\n' otto-gcda-check`` before ``find``
(``otto.host.product.gcda_check_cmd``), and its parser reads only what
follows the marker. A double that answers the check must print the frame
too, exactly as a shell would. Restated here rather than imported from the
product, so a change to the script turns these doubles RED.

The reset's removal line (``otto.host.product.gcda_delete_cmd``) is restated
the same way, as :func:`delete_line`.
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


def delete_line(cov_dir: str) -> str:
    """The reset's guarded removal line for *cov_dir*, as the pins spell it."""
    q = shlex.quote(cov_dir)
    find = f"find {q} -name '*.gcda' -type f -exec rm -f {{}} \\; ! -path '*\n*' -print"
    ancestors = (
        "while IFS= read -r f; do d=${f%/*}; "
        f"while case $d in {q}/*) true;; *) false;; esac; "
        "do printf '%s\\n' \"$d\"; d=${d%/*}; done; done"
    )
    prune = 'while IFS= read -r d; do rmdir "$d" 2>/dev/null; done'
    return (
        f"! test -d {q} || "
        f'{{ __otto_gcda_rc=$( {{ {{ {find}; echo "$?" >&3; }} | {ancestors} | '
        "{ LC_ALL=C sort -ru 2>/dev/null || while IFS= read -r _; do :; done; } | "
        f'{prune}; }} 3>&1 ); test "$__otto_gcda_rc" = 0; }}'
    )
