"""Hunk remap engine: line mapping across -U0 diffs."""

from otto.coverage.capture.remap import Hunk, LineRemapper, parse_u0_hunks

# Diff: 3 lines inserted after old line 2 (new lines 3-5).
INSERT = """--- a/f.c
+++ b/f.c
@@ -2,0 +3,3 @@
+a
+b
+c
"""

# Diff: old lines 4-5 deleted.
DELETE = """--- a/f.c
+++ b/f.c
@@ -4,2 +3,0 @@
-x
-y
"""

# Diff: old line 2 replaced by new lines 2-3, and old line 10 deleted.
MIXED = """--- a/f.c
+++ b/f.c
@@ -2,1 +2,2 @@
-old
+new1
+new2
@@ -10,1 +11,0 @@
-gone
"""


def test_parse_hunks() -> None:
    hunks = parse_u0_hunks(MIXED)
    assert hunks == [Hunk(2, 1, 2, 2), Hunk(10, 1, 11, 0)]


def test_insertion_new_to_old() -> None:
    r = LineRemapper(parse_u0_hunks(INSERT))
    assert r.new_to_old(1) == 1
    assert r.new_to_old(2) == 2
    assert r.new_to_old(3) is None  # inserted
    assert r.new_to_old(5) is None  # inserted
    assert r.new_to_old(6) == 3  # shifted by +3


def test_insertion_old_to_new() -> None:
    r = LineRemapper(parse_u0_hunks(INSERT))
    assert r.old_to_new(2) == 2
    assert r.old_to_new(3) == 6


def test_deletion_both_ways() -> None:
    r = LineRemapper(parse_u0_hunks(DELETE))
    assert r.new_to_old(3) == 3
    assert r.new_to_old(4) == 6  # old 4,5 gone; new 4 is old 6
    assert r.old_to_new(4) is None  # deleted
    assert r.old_to_new(6) == 4


def test_mixed_cumulative() -> None:
    r = LineRemapper(parse_u0_hunks(MIXED))
    assert r.new_to_old(1) == 1
    assert r.new_to_old(2) is None  # replacement
    assert r.new_to_old(3) is None
    assert r.new_to_old(4) == 3  # +1 offset after first hunk
    assert r.old_to_new(10) is None  # deleted
    assert r.old_to_new(11) == 11  # +1 then -1 -> net 0
    assert r.new_to_old(11) == 11


def test_empty_diff_is_identity() -> None:
    r = LineRemapper(parse_u0_hunks(""))
    assert r.new_to_old(42) == 42
    assert r.old_to_new(42) == 42


# --- Boundary regressions found while implementing this task -----------------
#
# The brief's reference LineRemapper decided "have we passed this hunk" using
# each hunk's *own* same-side start/count, with a manual +1 fix-up whenever
# that side's count is 0 (git's "position is the line before" convention).
# That fix-up is correct for a lone zero-count hunk (DELETE above: new_start=3
# is itself untouched, the shift begins at new line 4) but wrong once a prior
# hunk has already shifted the cumulative offset (MIXED's second hunk above:
# new_start=11 there needs the shift to apply *at* line 11, not line 12) --
# the same "start, count=0" shape needs opposite treatment depending on
# what preceded it. `test_mixed_cumulative`'s `new_to_old(11) == 11` catches
# this: the reference implementation returns 10.
#
# The fix computes each hunk's "passed" threshold from the *opposite* side's
# start+count translated through the running cumulative offset, instead of
# the same-side zero-count marker. That is well-defined regardless of which
# convention a given zero-count marker happens to encode, so it is verified
# below both against a diff shaped like the real git output for this exact
# scenario (git actually emits "+10,0" here, not the brief's "+11,0" -- see
# REAL_MULTI_HUNK) and against additional zero-count edge cases.

# Diff shaped exactly like real `git diff -U0` output for: old line 2 replaced
# by 2 new lines (net +1), then old line 10 deleted alone further down.
# Verified against actual git output (not hand-authored): unlike the brief's
# MIXED fixture, git's own zero-count convention places the deletion marker
# at "+10,0" (the line immediately after the still-open +1 shift), not
# "+11,0".
REAL_MULTI_HUNK = """--- a/f.c
+++ b/f.c
@@ -2 +2,2 @@
-old
+new1
+new2
@@ -10 +10,0 @@ old
-gone
"""

# Diff with two hunks that touch (hunk 2's old range starts exactly where
# hunk 1's ends) -- no untouched gap between them.
ADJACENT = """--- a/f.c
+++ b/f.c
@@ -2 +2 @@
-old
+new
@@ -3,2 +3,0 @@
-x
-y
"""

# Diff inserting 3 lines at the very start of the file (git's @@ -0,0 ... @@
# convention: old_start=0 since there is no "line 0" to anchor before).
INSERT_AT_START = """--- a/f.c
+++ b/f.c
@@ -0,0 +1,3 @@
+x
+y
+z
"""

# Diff deleting the very first line (git's @@ ... +0,0 @@ convention:
# new_start=0, the mirror image of INSERT_AT_START).
DELETE_FIRST_LINE = """--- a/f.c
+++ b/f.c
@@ -1 +0,0 @@
-a
"""


def test_parse_hunk_with_omitted_counts_on_both_sides() -> None:
    # "@@ -2 +2,2 @@" and "@@ -10 +10,0 @@" both omit the old-side count,
    # which git only does when it's exactly 1.
    hunks = parse_u0_hunks(REAL_MULTI_HUNK)
    assert hunks == [Hunk(2, 1, 2, 2), Hunk(10, 1, 10, 0)]


def test_real_git_multi_hunk_zero_count_boundary() -> None:
    """Regression for the exact boundary the reference implementation got wrong.

    Pins new_to_old across a trailing pure-deletion hunk once a preceding
    hunk has already shifted the cumulative offset, using hunk positions
    verified against real `git diff -U0` output (not hand-authored).
    """
    r = LineRemapper(parse_u0_hunks(REAL_MULTI_HUNK))
    assert r.new_to_old(1) == 1  # before either hunk
    assert r.new_to_old(2) is None  # replacement
    assert r.new_to_old(3) is None
    assert r.new_to_old(9) == 8  # +1 offset from hunk 1
    assert r.new_to_old(10) == 9  # last untouched line before the deletion
    assert r.new_to_old(11) == 11  # deletion's -1 cancels hunk 1's +1: net 0
    assert r.old_to_new(10) is None  # "gone" was deleted
    assert r.old_to_new(11) == 11


def test_adjacent_hunks_no_gap() -> None:
    r = LineRemapper(parse_u0_hunks(ADJACENT))
    assert r.new_to_old(1) == 1  # untouched, before hunk 1
    assert r.new_to_old(2) is None  # replaced in place
    assert r.new_to_old(3) == 5  # old 3,4 deleted; new 3 is old 5
    assert r.new_to_old(4) == 6
    assert r.old_to_new(2) is None
    assert r.old_to_new(4) is None  # inside the deletion
    assert r.old_to_new(5) == 3
    assert r.old_to_new(6) == 4


def test_insertion_at_line_one() -> None:
    r = LineRemapper(parse_u0_hunks(INSERT_AT_START))
    assert r.new_to_old(1) is None  # inserted
    assert r.new_to_old(3) is None
    assert r.new_to_old(4) == 1  # first original line, shifted by +3
    assert r.old_to_new(1) == 4


def test_deletion_of_first_line() -> None:
    r = LineRemapper(parse_u0_hunks(DELETE_FIRST_LINE))
    assert r.old_to_new(1) is None  # deleted
    assert r.old_to_new(2) == 1  # shifted by -1
    assert r.new_to_old(1) == 2
