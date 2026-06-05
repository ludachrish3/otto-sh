"""Embedded (Zephyr LLEXT) coverage OttoSuite.

The embedded analogue of repo1's ``TestCoverageProduct``. Instead of compiling a
host binary and emitting ``.gcda`` to a filesystem, it loads a
coverage-instrumented LLEXT extension (``product/``) onto each embedded coverage
host, runs its operations over the console to exercise code paths, and lets
``otto test --cov``'s :class:`~otto.coverage.fetcher.embedded.EmbeddedGcdaCollector`
trigger ``cov_dump`` and decode the serial hexdump into ``.gcda``.

Run against the standard ``embedded`` lab; the coverage host(s) are selected by
the repo-declared ``[coverage].hosts`` regex (not a dedicated lab), e.g.::

    otto test --cov --lab embedded TestEmbeddedCoverage
    otto cov report <output_dir> --report ./report

Per-host lifecycle: ``llext load_hex`` (install) -> ``call_fn cov_init`` ->
``call_fn <op>`` (exercise) -> [collector: ``call_fn cov_dump``] ->
``llext unload`` (teardown — skipped under ``--cov`` so the extension is still
loaded when the collector dumps it).

The suite rebuilds the extension (``product/build.sh``) before loading it; the
resulting ``cov_ext.stripped.llext`` is read from ``[coverage.embedded].build_dir``
on the machine running ``otto test``.
"""

import binascii
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from otto.configmodule.configmodule import all_hosts, getConfigModule
from otto.host import LocalHost
from otto.host.embeddedHost import EmbeddedHost
from otto.logger import getOttoLogger
from otto.suite import OttoSuite, register_suite
from otto.suite.plugin import otto_cov_key
from otto.utils import Status

logger = getOttoLogger()

PRODUCT_DIR = Path(__file__).resolve().parent.parent / "product"
BUILD_SCRIPT = PRODUCT_DIR / "build.sh"


def _embedded_cov_config() -> dict:
    """Return the ``[coverage.embedded]`` table from the first repo declaring one."""
    for repo in getConfigModule().repos:
        embedded = (repo.settings.get("coverage") or {}).get("embedded")
        if embedded:
            return embedded
    return {}


def _extension() -> str:
    return _embedded_cov_config().get("extension", "cov_ext")


def _extension_hex() -> str:
    """Hex of the pre-built, stripped LLEXT extension (sent via ``load_hex``)."""
    build_dir = _embedded_cov_config().get("build_dir")
    if not build_dir:
        raise RuntimeError("[coverage.embedded].build_dir is not configured")
    llext = Path(build_dir) / "zephyr" / f"{_extension()}.stripped.llext"
    if not llext.exists():
        raise RuntimeError(
            f"extension not built: {llext} — build product/ first (see its README)"
        )
    return binascii.hexlify(llext.read_bytes()).decode()


async def _build_extension() -> None:
    """Rebuild the LLEXT coverage extension into ``[coverage.embedded].build_dir``.

    The embedded analogue of repo1's ``_compile_product``: the suite keeps the
    product up to date rather than trusting a stale pre-built artifact. Runs
    ``product/build.sh`` on the machine executing the suite (the dev VM, where
    ``build_dir`` lives) and hard-fails if the build can't run or errors. The
    script is idempotent, so a pre-existing build dir is fine.
    """
    build_dir = _embedded_cov_config().get("build_dir")
    if not build_dir:
        raise RuntimeError("[coverage.embedded].build_dir is not configured")
    localhost = LocalHost()
    try:
        result = await localhost.oneshot(f"bash {BUILD_SCRIPT} {build_dir}", timeout=900)
        if result.status != Status.Success:
            raise RuntimeError(
                f"extension build failed (see {BUILD_SCRIPT}):\n{result.output}"
            )
        logger.info("Rebuilt %s into %s", _extension(), build_dir)
    finally:
        await localhost.close()


def _coverage_host_pattern() -> 're.Pattern[str] | None':
    """Return the repo-declared ``[coverage].hosts`` selector, compiled (or ``None``)."""
    for repo in getConfigModule().repos:
        hosts = (repo.settings.get("coverage") or {}).get("hosts")
        if hosts:
            return re.compile(hosts)
    return None


def _embedded_hosts() -> list[EmbeddedHost]:
    """Return the embedded coverage host(s) in the active lab.

    With the coverage host folded into the standard ``embedded`` lab, the
    ``[coverage].hosts`` regex — the same selector the collector uses — picks
    which embedded hosts this suite loads the instrumented extension onto, so
    the plain embedded test hosts are left untouched.
    """
    pattern = _coverage_host_pattern()
    return [h for h in all_hosts(pattern=pattern) if isinstance(h, EmbeddedHost)]


async def _call(host: EmbeddedHost, fn: str, timeout: float = 60) -> None:
    """Invoke an exported extension entry point over the console."""
    ext = _extension()
    result = await host.oneshot(f"llext call_fn {ext} {fn}", timeout=timeout)
    if result.status != Status.Success:
        raise RuntimeError(f"call_fn {fn} failed on {host.id}: {result.output}")


async def _drain_unload(host: EmbeddedHost, ext: str, max_rounds: int = 16) -> None:
    """Fully evict *ext* from the device so the next ``load_hex`` installs fresh
    bytes instead of refcount-bumping a resident copy.

    Zephyr's ``llext_load`` does **not** replace an already-loaded extension: if
    one of the same name is resident it only increments a use-count and returns
    the *old* count (which ``cmd_llext_load_hex`` misreports as "Failed to load
    ... return code N", N>=1). ``llext_unload`` is the matching decrement and
    frees the extension only when the count reaches 0. A ``--cov`` run leaves the
    extension loaded (teardown skips the unload so the collector can dump it),
    and every ``load_hex`` that finds it resident bumps the count further, so a
    single unload no longer evicts it. The device then keeps serving the old
    build while the rebuilt ``.gcno`` carries a new stamp, and the report fails
    with a ``.gcda`` "stamp mismatch with notes file".

    Unload in a loop until the shell reports "No such extension", which is what
    ``cmd_llext_unload`` prints once ``llext_by_name`` returns NULL (use-count 0).
    Harmless when nothing is loaded — the first round returns immediately.
    """
    for _ in range(max_rounds):
        result = await host.oneshot(f"llext unload {ext}", timeout=20)
        if "No such extension" in result.output:
            return
    raise RuntimeError(
        f"could not fully unload {ext} on {host.id} after {max_rounds} rounds "
        f"(llext use-count never reached 0): {result.output}"
    )


@dataclass
class _Options:
    pass


@register_suite()
@pytest.mark.asyncio(loop_scope="class")
class TestEmbeddedCoverage(OttoSuite[_Options]):
    """Exercise the LLEXT coverage product over the console on each embedded
    coverage host, leaving the extension loaded for ``--cov`` collection.
    """

    Options = _Options

    @pytest_asyncio.fixture(autouse=True, scope="class", loop_scope="class")
    async def _load_extension(self, request):
        """Rebuild, then load + initialise the extension on every embedded host;
        unload on teardown (unless ``--cov`` needs it kept for the post-test dump).
        """
        hosts = _embedded_hosts()
        if not hosts:
            pytest.skip("no embedded coverage hosts in the active lab")
        request.cls._hosts = hosts

        # Keep the product up to date — repo1's TestCoverageProduct compiles its
        # binary the same way. Rebuild before reading the artifact below so the
        # loaded extension always reflects the current source.
        await _build_extension()

        ext = _extension()
        hexstr = _extension_hex()
        for host in hosts:
            # Evict any resident copy first so load_hex installs the freshly-built
            # bytes (see _drain_unload): otherwise llext_load refcount-bumps the
            # stale build, the rebuilt .gcno's new stamp no longer matches the
            # dumped .gcda, and `otto cov report` fails with a stamp mismatch.
            await _drain_unload(host, ext)
            result = await host.oneshot(f"llext load_hex {ext} {hexstr}", timeout=120)
            # cmd_llext_load_hex always returns shell-success (0) even on a load
            # error, so the exit status can't be trusted — check the printed
            # outcome. A clean device prints "Successfully loaded extension".
            if "Successfully loaded extension" not in result.output:
                raise RuntimeError(f"load_hex did not load {ext} on {host.id}: {result.output}")
            # Run the gcov constructor so cov_dump has a registered gcov_info.
            await _call(host, "cov_init")
            logger.info("Loaded %s on %s", ext, host.id)

        yield

        cov_active = request.config.stash.get(otto_cov_key, False)
        if not cov_active:
            for host in hosts:
                await host.oneshot(f"llext unload {ext}", timeout=20)
                logger.info("Unloaded %s from %s", ext, host.id)

    @pytest.mark.integration
    async def test_clamp_below(self) -> None:
        """`math_clamp` value-below-lo branch — on all hosts."""
        for host in self._hosts:
            await _call(host, "op_clamp_lo")

    @pytest.mark.integration
    async def test_clamp_in_range(self) -> None:
        """`math_clamp` in-range branch — on all hosts."""
        for host in self._hosts:
            await _call(host, "op_clamp_in")

    @pytest.mark.integration
    async def test_divide(self) -> None:
        """`math_div` success branch — on all hosts."""
        for host in self._hosts:
            await _call(host, "op_div_ok")

    @pytest.mark.integration
    async def test_divide_by_zero_one_host(self) -> None:
        """`math_div` divide-by-zero branch — on the first host only.

        Mirrors repo1: running this branch on a single instance means it is
        covered only once coverage is *merged* across instances. Demonstrating
        merge > any single instance needs >= 2 coverage instances; with one
        coverage host today this still exercises the branch, just without the
        cross-instance delta.
        """
        await _call(self._hosts[0], "op_div_zero")
