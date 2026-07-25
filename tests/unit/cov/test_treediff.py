"""parse_multifile_u0: split one `git diff -M -w -U0` stream into per-file diffs."""

from otto.coverage.capture.remap import Hunk
from otto.coverage.capture.treediff import FileDiff, parse_multifile_u0

MODIFIED = """\
diff --git a/src/a.c b/src/a.c
index 1111111..2222222 100644
--- a/src/a.c
+++ b/src/a.c
@@ -3,0 +4,2 @@ int f(void)
+    x();
+    y();
@@ -10 +12 @@ int g(void)
-    old();
+    new();
"""

RENAMED_CLEAN = """\
diff --git a/src/old.c b/src/new.c
similarity index 100%
rename from src/old.c
rename to src/new.c
"""

RENAMED_EDITED = """\
diff --git a/src/tcp.c b/src/net/tcp.c
similarity index 91%
rename from src/tcp.c
rename to src/net/tcp.c
index 3333333..4444444 100644
--- a/src/tcp.c
+++ b/src/net/tcp.c
@@ -7 +7 @@
-    a();
+    b();
"""

DELETED = """\
diff --git a/src/gone.c b/src/gone.c
deleted file mode 100644
index 5555555..0000000
--- a/src/gone.c
+++ /dev/null
@@ -1,3 +0,0 @@
-int gone(void)
-{
-}
"""

ADDED = """\
diff --git a/src/fresh.c b/src/fresh.c
new file mode 100644
index 0000000..6666666
--- /dev/null
+++ b/src/fresh.c
@@ -0,0 +1,2 @@
+int fresh(void)
+{
"""

QUOTED = (
    'diff --git "a/sp ace.c" "b/sp ace.c"\n'
    '--- "a/sp ace.c"\n+++ "b/sp ace.c"\n@@ -1 +1 @@\n-a\n+b\n'
)


class TestParseMultifileU0:
    def test_modified_file_keeps_all_hunks(self):
        out = parse_multifile_u0(MODIFIED)
        fd = out["src/a.c"]
        assert fd == FileDiff(
            old_path="src/a.c",
            new_path="src/a.c",
            hunks=[Hunk(3, 0, 4, 2), Hunk(10, 1, 12, 1)],
        )

    def test_clean_rename_has_no_hunks(self):
        fd = parse_multifile_u0(RENAMED_CLEAN)["src/old.c"]
        assert fd.new_path == "src/new.c"
        assert fd.hunks == []

    def test_edited_rename_keeps_hunks_under_old_path_key(self):
        out = parse_multifile_u0(RENAMED_EDITED)
        fd = out["src/tcp.c"]
        assert fd.new_path == "src/net/tcp.c"
        assert fd.hunks == [Hunk(7, 1, 7, 1)]

    def test_deleted_file_maps_to_none(self):
        fd = parse_multifile_u0(DELETED)["src/gone.c"]
        assert fd.new_path is None

    def test_added_file_is_ignored(self):
        # /dev/null old side: nothing was anchored there; not our concern.
        assert parse_multifile_u0(ADDED) == {}

    def test_quoted_paths_are_unquoted(self):
        assert "sp ace.c" in parse_multifile_u0(QUOTED)

    def test_concatenated_stream_splits_per_file(self):
        out = parse_multifile_u0(MODIFIED + RENAMED_EDITED + DELETED)
        assert set(out) == {"src/a.c", "src/tcp.c", "src/gone.c"}

    def test_empty_input(self):
        assert parse_multifile_u0("") == {}

    def test_hunk_content_lines_never_shadow_headers(self):
        # A deleted SQL comment renders as `--- ...` inside the hunk body;
        # header sentinels must only be honored before the first @@ line.
        diff = (
            "diff --git a/src/q.sql b/src/q.sql\n"
            "index 1111111..2222222 100644\n"
            "--- a/src/q.sql\n"
            "+++ b/src/q.sql\n"
            "@@ -5 +5 @@\n"
            "--- old note\n"
            "+-- new note kept\n"
        )
        out = parse_multifile_u0(diff)
        assert set(out) == {"src/q.sql"}
        assert out["src/q.sql"].new_path == "src/q.sql"
