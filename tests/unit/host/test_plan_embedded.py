"""The embedded kind's plan is the load, the calls and the unload its hooks make."""

from unittest.mock import AsyncMock

import pytest

from otto.declared import DeclaredEntry
from otto.host import ZephyrHost, embedded_kind  # noqa: F401 — import registers the kind
from otto.host.binary_loader import LlextHexLoader
from otto.host.element import Element
from otto.host.embedded_kind import EmbeddedProduct
from otto.host.product import PRODUCT_KIND_BUILDER
from otto.logger.mode import LogMode
from otto.result import CommandResult, Result
from otto.utils import Status


def _ext(host, tmp_path, name="fw", **params):
    obj = tmp_path / "fw.llext"
    obj.write_bytes(b"\x00")
    entry = DeclaredEntry(
        name=name,
        kind="embedded",
        seam="products",
        owner="r1",
        base_dir=tmp_path,
        match={},
        params={"artifact": "fw.llext", **params},
    )
    return PRODUCT_KIND_BUILDER.build([entry], host)[0]


@pytest.mark.asyncio
async def test_embedded_plan_is_the_load_the_calls_and_the_unload(embedded_plan_recorder, tmp_path):
    host = embedded_plan_recorder
    product = _ext(host, tmp_path, call_after_load=["gcov_init", "start"])
    plan = product.plan(host)
    assert (await product.stage(host)).is_ok
    assert host.take() == [] == plan.stage
    assert (await product.install(host)).is_ok
    assert (
        host.take()
        == plan.install
        == [
            f"LOAD {tmp_path}/fw.llext as fw",
            "llext call_fn fw gcov_init",
            "llext call_fn fw start",
        ]
    )
    assert (await product.uninstall(host)).is_ok
    assert host.take() == plan.uninstall == ["llext unload fw"]
    assert plan.unchecked == [
        "the device's answer to the load and to each unload round (at most 16 rounds)"
    ]


@pytest.mark.asyncio
async def test_unusual_but_legal_names_pin_the_exact_spelling(embedded_plan_recorder, tmp_path):
    host = embedded_plan_recorder
    product = _ext(host, tmp_path, name="fw-2", call_after_load=["gcov_init"])
    plan = product.plan(host)
    assert (await product.install(host)).is_ok
    assert (
        host.take()
        == plan.install
        == [
            f"LOAD {tmp_path}/fw.llext as fw-2",
            "llext call_fn fw-2 gcov_init",
        ]
    )
    assert (await product.uninstall(host)).is_ok
    assert host.take() == plan.uninstall == ["llext unload fw-2"]


def test_a_host_without_a_loader_is_a_named_gap(embedded_recording_host, tmp_path):
    """A code provider hands over an ``EmbeddedProduct`` without the factory's loader check.

    ``[[products]]`` entries never reach this branch (the factory refuses a
    loaderless host), but ``apply_product_providers`` attaches whatever a
    provider returns, so the plan must name the gap rather than raise.
    """
    host = embedded_recording_host  # no `loader` attribute
    plan = EmbeddedProduct(artifact=tmp_path / "fw.llext", name="fw").plan(host)
    assert plan.install == plan.uninstall == []
    assert plan.unchecked == [
        (
            "h1 has no binary loader: "
            "the install and the uninstall are refused before the device is touched"
        )
    ]


@pytest.mark.asyncio
async def test_the_real_uninstall_on_a_loaderless_host_still_asks_the_host_to_unload(tmp_path):
    """The refusal lives in ``plan()`` only: a real run's ``uninstall`` is what it always was."""

    class _Loaderless:
        id = "h1"

        def __init__(self) -> None:
            self.unloaded: list[str] = []

        async def unload(self, name: str) -> Result:
            self.unloaded.append(name)
            return Result(Status.Success)

    host = _Loaderless()
    product = EmbeddedProduct(artifact=tmp_path / "fw.llext", name="fw")
    assert (await product.uninstall(host)).is_ok
    assert host.unloaded == ["fw"]


def _real_zephyr_host() -> ZephyrHost:
    """A real ``ZephyrHost`` with an LLEXT loader and a mocked session (as test_embedded_host)."""
    host = ZephyrHost(
        ip="192.0.2.1", element=Element("zephyr37_fat"), log=LogMode.QUIET, loader=LlextHexLoader()
    )
    host._connections = None  # type: ignore[assignment]  # so __del__ does not churn a loop
    host._session_mgr = AsyncMock()
    return host


def _answer(output: str) -> CommandResult:
    return CommandResult(status=Status.Success, value=output, command="c", retcode=0)


@pytest.mark.asyncio
async def test_the_plans_unload_line_is_what_the_real_host_sends(tmp_path):
    """The recorder stubs ``unload``; drive the real ``EmbeddedHost.unload`` to pin its command."""
    host = _real_zephyr_host()
    host._session_mgr.run_cmd.return_value = _answer("No such extension fw-2")
    product = _ext(host, tmp_path, name="fw-2")
    assert (await product.uninstall(host)).is_ok
    sent = [call.args[0] for call in host._session_mgr.run_cmd.await_args_list]
    assert sent == product.plan(host).uninstall == ["llext unload fw-2"]


@pytest.mark.asyncio
async def test_the_plans_load_line_names_what_the_real_host_loads(tmp_path):
    """The real load carries the payload hex-encoded; the name it loads under is the plan's."""
    host = _real_zephyr_host()
    host._session_mgr.run_cmd.return_value = _answer("Successfully loaded extension fw-2")
    product = _ext(host, tmp_path, name="fw-2")
    assert (await product.install(host)).is_ok
    sent = host._session_mgr.run_cmd.await_args_list[0].args[0]
    assert product.plan(host).install[0] == f"LOAD {product.artifact} as fw-2"
    assert sent.startswith(host.loader.load_command("fw-2", b"")), sent
    assert sent == host.loader.load_command("fw-2", product.artifact.read_bytes())
