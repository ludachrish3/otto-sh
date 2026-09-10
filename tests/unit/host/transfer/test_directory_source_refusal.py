"""A directory handed to a non-recursive put is refused by name, on every
backend, before any byte moves."""

from pathlib import Path

import pytest

from otto.host.local_host import LocalHost
from otto.host.transfer.base import DIRECTORY_SOURCE_HINT
from otto.utils import Status


@pytest.mark.asyncio
async def test_directory_source_without_recursive_is_refused_before_any_byte_moves(tmp_path: Path):
    host = LocalHost()
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.txt").write_text("a")
    plain = tmp_path / "plain.txt"
    plain.write_text("p")
    dest = tmp_path / "dest"
    dest.mkdir()

    result = await host.put([plain, tree], dest)

    assert result.status == Status.Error
    assert result.value[tree].status == Status.Error
    assert result.value[tree].msg == f"{tree}: {DIRECTORY_SOURCE_HINT}"
    assert "-r" in result.value[tree].msg
    assert result.value[plain].status == Status.Skipped
    assert not (dest / "plain.txt").exists(), "a refused batch must move nothing"
