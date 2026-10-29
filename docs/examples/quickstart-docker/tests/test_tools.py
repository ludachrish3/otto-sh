"""End-to-end tests: each tool runs once per test, writing coverage as it exits."""

import pytest
from quickstart_docker.tools import run_tool


class TestTools:
    """wordcount and shout in the compose container, banner in its own image."""

    @pytest.mark.parametrize(("words", "count"), [("one", 1), ("one two", 2), ("one two three", 3)])
    async def test_wordcount_counts_words(self, words: str, count: int) -> None:
        """The word count printed matches the words given."""
        assert await run_tool("wordcount", words) == str(count)

    async def test_shout_capitalises(self) -> None:
        """The arguments come back in capitals."""
        assert await run_tool("shout", "hello world") == "HELLO WORLD"

    async def test_banner_counts_then_repeats(self) -> None:
        """The singleton image's tool prints the word count, then the words."""
        assert await run_tool("banner", "hello docker world") == "[3] hello docker world"
