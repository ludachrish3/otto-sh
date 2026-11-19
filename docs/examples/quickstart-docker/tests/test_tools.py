"""End-to-end tests: each tool runs once per test, and one test walks the fleet."""

import pytest
from quickstart_docker.tools import CONTAINER, run_tool

from otto.host import DockerContainerHost
from otto.lab import all_hosts


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

    async def test_compose_container_is_a_fleet_host_on_request(self) -> None:
        """A walk that opts in to containers finds the compose container."""
        containers = [
            h.id for h in all_hosts(include_containers=True) if isinstance(h, DockerContainerHost)
        ]
        assert CONTAINER in containers
