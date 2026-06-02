"""Embedded (Zephyr LLEXT) coverage OttoSuite.

The embedded analogue of repo1's ``TestCoverageProduct``. Instead of compiling a
host binary and emitting ``.gcda`` to a filesystem, it loads a
coverage-instrumented LLEXT extension (``product/``) onto each embedded coverage
host, runs its operations over the console to exercise code paths, and lets
``otto test --cov``'s :class:`~otto.coverage.fetcher.embedded.EmbeddedGcdaCollector`
trigger ``cov_dump`` and decode the serial hexdump into ``.gcda``.

Run scoped to the coverage host(s) (the ``embedded-cov`` lab), e.g.::

    otto test --cov --lab embedded-cov TestEmbeddedCoverage
    otto cov report <output_dir> --report ./report

Per-host lifecycle: ``llext load_hex`` (install) -> ``call_fn cov_init`` ->
``call_fn <op>`` (exercise) -> [collector: ``call_fn cov_dump``] ->
``llext unload`` (teardown — skipped under ``--cov`` so the extension is still
loaded when the collector dumps it).

The extension must be pre-built (see ``product/README.md``); its
``cov_ext.stripped.llext`` is read from ``[coverage.embedded].build_dir``, which
must be reachable on the machine running ``otto test``.
"""

import binascii
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from otto.configmodule.configmodule import all_hosts, getConfigModule
from otto.host.embeddedHost import EmbeddedHost
from otto.logger import getOttoLogger
from otto.suite import OttoSuite, register_suite
from otto.suite.plugin import otto_cov_key
from otto.utils import Status

logger = getOttoLogger()


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


def _embedded_hosts() -> list[EmbeddedHost]:
    """Embedded hosts in the active lab. Scope the run (``--lab embedded-cov``)
    so this is the coverage host(s) and not the plain embedded test hosts.
    """
    return [h for h in all_hosts() if isinstance(h, EmbeddedHost)]


async def _call(host: EmbeddedHost, fn: str, timeout: float = 60) -> None:
    """Invoke an exported extension entry point over the console."""
    ext = _extension()
    result = await host.oneshot(f"llext call_fn {ext} {fn}", timeout=timeout)
    if result.status != Status.Success:
        raise RuntimeError(f"call_fn {fn} failed on {host.id}: {result.output}")


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
        """Load + initialise the extension on every embedded host; unload on
        teardown (unless ``--cov`` needs it kept for the post-test dump).
        """
        hosts = _embedded_hosts()
        if not hosts:
            pytest.skip("no embedded coverage hosts in the active lab")
        request.cls._hosts = hosts

        ext = _extension()
        hexstr = _extension_hex()
        for host in hosts:
            result = await host.oneshot(f"llext load_hex {ext} {hexstr}", timeout=120)
            if result.status != Status.Success:
                raise RuntimeError(f"load_hex failed on {host.id}: {result.output}")
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
