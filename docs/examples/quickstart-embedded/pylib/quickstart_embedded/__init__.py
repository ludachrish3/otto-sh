"""The embedded track's project code: call an exported function of the loaded extension."""

from otto.lab import get_host


class CallError(RuntimeError):
    """The extension's function did not run."""


async def call_fn(product: str, fn: str) -> str:
    """Run ``fn`` of the extension named ``product`` on dut; return what it printed."""
    dut = get_host("dut")
    loader = getattr(dut, "loader", None)
    if loader is None:
        raise CallError("dut declares no binary loader")
    result = await dut.exec(loader.call_command(product, fn))
    if loader.reports_not_loaded(result.value, product):
        raise CallError(f"extension {product} is not loaded on dut")
    if not result.is_ok:
        raise CallError(f"{product} {fn} failed on dut: {result.value!r}")
    return result.value
