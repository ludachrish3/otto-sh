"""End-to-end tests: the module on dut counts the words of each line written to it."""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from quickstart_kmod.wordcount import count_words

from otto.lab import get_host


@pytest.mark.ensure("installed")
class TestWordcount:
    """The loaded module, one line per test."""

    @pytest_asyncio.fixture(scope="class", autouse=True)
    @classmethod
    async def _unload_after(cls) -> AsyncIterator[None]:
        """Unload the module after the class: its exit routine's counters reach this run."""
        yield
        result = await get_host("dut").uninstall()
        if not result.is_ok:
            raise RuntimeError(f"unloading the module on dut failed: {result.msg}")

    @pytest.mark.parametrize(("line", "words"), [("one", 1), ("one two", 2), ("one  two three", 3)])
    async def test_counts_words(self, line: str, words: int) -> None:
        """The count read back matches the words written."""
        assert await count_words(line) == words
