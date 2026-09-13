"""The ensure fixture converges with the suite's options as the source."""

import pytest

from otto.params import OptionsSource
from otto.suite import pytest_plugin


@pytest.mark.asyncio
async def test_converge_passes_the_suite_options_as_an_instance_source(monkeypatch) -> None:
    seen = {}

    async def fake_ensure_installed(source):
        seen["source"] = source
        from otto.result import Result
        from otto.utils import Status

        return Result(Status.Success)

    from otto import project

    monkeypatch.setattr(project, "ensure_installed", fake_ensure_installed)
    marker = object()
    await pytest_plugin._converge("installed", marker)
    assert isinstance(seen["source"], OptionsSource)
    assert seen["source"].instance is marker
