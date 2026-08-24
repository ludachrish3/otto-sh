"""Delete excluded lines and branches from a merged CoverageStore.

Deletion rather than flagging is what makes this correct with no
downstream changes: every stats path computes over records that are simply
not there.
"""

import logging
from pathlib import Path

from ..store.model import CoverageStore
from .paths import path_rule_matches
from .rules import BUILTIN_MARKER_RULES, ExclusionRule, PathRule
from .scan import scan_source

logger = logging.getLogger(__name__)


def apply_exclusions(store: CoverageStore, rules: "list[ExclusionRule]", root: Path) -> None:
    """Remove every line and branch the rules exclude, in place.

    The built-in ``LCOV_EXCL_*`` families always apply, so the standard
    markers are enforced by otto itself rather than only by ``geninfo`` on
    the paths otto captures.

    *root* is resolved before any path rule sees it:
    :meth:`~otto.coverage.store.model.CoverageStore.get_or_create_file`
    canonicalises every path that exists on disk, so an unresolved root
    that traverses a symlink makes ``relative_to`` raise and every
    relative glob match nothing.

    **Once per store.** Re-applying this to an already-filtered store is
    unsupported and will under-report: the ``stat = "branch"`` path arm is a
    function of pre-clear branch state, which the first run consumes, so a
    second run finds no branches to attribute and reports none.
    """
    all_rules = list(BUILTIN_MARKER_RULES) + list(rules)
    path_rules = [r for r in all_rules if isinstance(r, PathRule)]
    source_rules = [r for r in all_rules if not isinstance(r, PathRule)]
    resolved_root = root.resolve()

    for record in list(store.files()):
        # Re-entry: this stage OWNS both exclusion fields, so a record arriving
        # with a previous run's values — CoverageStore.load restores both — must
        # be replaced, not accumulated onto. Both are cleared HERE, ahead of the
        # source read, so they still agree on the one exit that never reaches
        # the rebind below: a file whose source has become unreadable reports NO
        # verdict, rather than the previous run's verdict in one field and none
        # in the other. Clearing here also leaves the path-rule arm free to
        # write onto the record, which is what survives that same exit.
        record.excluded_lines = set()
        record.branch_excluded_lines = set()
        dropped = False
        for rule in path_rules:
            if not path_rule_matches(record.path, resolved_root, rule):
                continue
            if rule.stat == "line":
                store.remove_file(record.path)
                dropped = True
                break
            # Which lines HAD branches is only knowable before the clear, so
            # it is recorded on the record now rather than recomputed later.
            record.branch_excluded_lines |= {
                lineno for lineno, line in record.lines.items() if line.branches
            }
            for line in record.lines.values():
                line.branches.clear()
        if dropped:
            continue

        try:
            source = record.path.read_text(errors="replace")
        except OSError as e:
            logger.warning("Could not read source %s (%s); its lines are all kept.", record.path, e)
            continue

        result = scan_source(source, source_rules)
        for lineno in result.lines:
            record.lines.pop(lineno, None)
        # No ``- result.lines`` here: those records were just popped, so the
        # None check below already covers them. The subtraction at the end of
        # the function is a different question and stays.
        for lineno in result.branch_lines:
            line = record.lines.get(lineno)
            if line is not None:
                line.branches.clear()
        record.excluded_lines = set(result.lines)
        # Union, not rebind: the path-rule arm above already recorded its own
        # cleared lines. A line deleted outright at ``stat="line"`` is not a
        # line "whose branches were excluded", so it leaves both sets.
        record.branch_excluded_lines = (
            record.branch_excluded_lines | result.branch_lines
        ) - result.lines
