"""Become root on test1 through the project's sudo proxy, and prove it."""

# doc: begin as-root
import asyncio
from pathlib import Path

import otto

HERE = Path(__file__).resolve().parent


async def main() -> None:
    """Report the session's user before, during and after the switch."""
    # open_context bootstraps the project first, so the `init` module has
    # registered the proxy before the lab loads and the cred is validated.
    async with otto.open_context(lab="unix", search_paths=[HERE / "lab_data"]):
        host = otto.get_host("test1")
        before = (await host.run("id -un")).only.value.strip()
        async with host.as_user("root"):
            during = (await host.run("id -un")).only.value.strip()
        after = (await host.run("id -un")).only.value.strip()
    print(f"{before} -> {during} -> {after}")


asyncio.run(main())
# doc: end as-root
