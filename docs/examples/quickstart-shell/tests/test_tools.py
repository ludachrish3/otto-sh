"""End-to-end tests: each tool runs once per test on dut, writing coverage as it exits."""

import pytest
from quickstart_shell.tools import run_tool


class TestTools:
    """The two command-line tools, one invocation per test."""

    @pytest.mark.parametrize(("words", "count"), [("one", 1), ("one two", 2), ("one two three", 3)])
    async def test_wordcount_counts_words(self, words: str, count: int) -> None:
        """The word count printed matches the words given."""
        assert await run_tool("wordcount", words) == str(count)

    async def test_shout_capitalises(self) -> None:
        """The arguments come back in capitals."""
        assert await run_tool("shout", "hello world") == "HELLO WORLD"
