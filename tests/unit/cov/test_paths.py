"""Tests for path correlator and auto-discovery."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from otto.coverage.correlator.paths import (
    PathCorrelator,
    PathMapping,
    discover_path_mappings,
)
from otto.utils import CommandStatus, Status


class TestPathMapping:

    def test_apply_matching(self):
        m = PathMapping("/build/workspace", "/home/user/src")
        assert m.apply("/build/workspace/foo.c") == "/home/user/src/foo.c"

    def test_apply_no_match(self):
        m = PathMapping("/build/workspace", "/home/user/src")
        assert m.apply("/other/path/foo.c") is None

    def test_apply_exact_prefix(self):
        m = PathMapping("/build", "/local")
        assert m.apply("/build/sub/file.c") == "/local/sub/file.c"


class TestPathCorrelator:

    def test_resolve_first_match_wins(self, tmp_path):
        (tmp_path / "a.c").touch()
        c = PathCorrelator([
            PathMapping("/first", str(tmp_path)),
            PathMapping("/second", str(tmp_path)),
        ])
        result = c.resolve("/first/a.c")
        assert result is not None
        assert result.name == "a.c"

    def test_resolve_fallthrough(self, tmp_path):
        (tmp_path / "a.c").touch()
        c = PathCorrelator([
            PathMapping("/nomatch", "/does/not/exist"),
            PathMapping("/real", str(tmp_path)),
        ])
        result = c.resolve("/real/a.c")
        assert result is not None

    def test_resolve_none_when_no_match(self):
        c = PathCorrelator([PathMapping("/x", "/y")])
        assert c.resolve("/z/file.c") is None

    def test_resolve_strict_raises(self):
        c = PathCorrelator([])
        with pytest.raises(FileNotFoundError):
            c.resolve_strict("/missing/file.c")


class TestDiscoverPathMappings:

    @pytest.mark.asyncio
    async def test_discovers_common_prefix(self, tmp_path):
        from otto.host.localHost import LocalHost

        info_content = (
            "SF:/build/ci/workspace/src/foo.c\n"
            "SF:/build/ci/workspace/src/bar.c\n"
            "SF:/build/ci/workspace/lib/baz.c\n"
        )
        info_file = tmp_path / "test.info"
        info_file.write_text(f"TN:\n{info_content}end_of_record\n")

        source_root = tmp_path / "myproject"
        source_root.mkdir()

        localhost = LocalHost()
        try:
            mappings = await discover_path_mappings(info_file, source_root, localhost)
            assert len(mappings) == 1
            assert mappings[0].from_prefix == "/build/ci/workspace"
            assert mappings[0].to_prefix == str(source_root.resolve())
        finally:
            await localhost.close()

    @pytest.mark.asyncio
    async def test_no_sf_lines(self, tmp_path):
        from otto.host.localHost import LocalHost

        info_file = tmp_path / "empty.info"
        info_file.write_text("TN:\nend_of_record\n")

        localhost = LocalHost()
        try:
            mappings = await discover_path_mappings(info_file, tmp_path, localhost)
            assert mappings == []
        finally:
            await localhost.close()
