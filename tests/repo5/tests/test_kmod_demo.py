"""OttoSuite driving otto_kmod_demo on the unix bed through the kmod products.

Both hosts run the common mix; test1 adds the drop-oldest policy, a parse
error and a range error, and a mid-suite dump; test2 adds the stop-marker
drain and a limit change. Both hosts' mixes end with one more ``enqueue`` so
the queue is non-empty when teardown unloads the module — otherwise
``demo_exit``'s ``if (queue.len)`` drain branch is never taken and the exit
dump only proves a free. Two paths stay uncovered on purpose (``drain`` with
an argument; ``limit`` below the queue length) so the report has something to
show as missed. Teardown UNINSTALLS the products — that is what makes the
exit routine's coverage reach the post-run fetch.

The common mix runs once per host inside the class-scoped ``_modules``
fixture, right after install, rather than as its own test — the two ONLY
mixes assume the queue state (and the "limit 4" cap) it leaves behind, so it
must run before either regardless of the order pytest-randomly picks for the
test methods below. ``test_common_mix`` only asserts against the read-back
line the fixture already captured. The two ONLY tests target different
hosts, so they stay independent of each other under any ordering.
"""

import re
import shlex

import pytest
import pytest_asyncio

from otto import options
from otto.config.fleet import all_hosts, do_for_all_hosts
from otto.host.unix_host import UnixHost
from otto.suite import OttoSuite

CTL = "/sys/kernel/debug/otto_kmod_demo/ctl"
DUMP = "/sys/kernel/debug/otto_kgcov/otto_kmod_demo/dump"
_HOSTS = re.compile(r"test[12]")

COMMON = [
    "limit 4",
    "enqueue 1",
    "enqueue 2",
    "enqueue 3",
    "enqueue 4",
    "enqueue 5",
    "drain",
    "policy lifo",
    "enqueue 7",
    "enqueue 8",
    "drain",
]
TEST1_FILL = [
    "policy drop-oldest",
    "enqueue 1",
    "enqueue 2",
    "enqueue 3",
    "enqueue 4",
    "enqueue 5",
    "drain",
]
TEST1_ERRORS = ["bogus", "limit 999"]  # -EINVAL, then -ERANGE
TEST1_EXIT_ENQUEUE = "enqueue 6"  # leaves the queue non-empty for the exit-time drain
TEST1_ONLY = [*TEST1_FILL, *TEST1_ERRORS, TEST1_EXIT_ENQUEUE]

TEST2_FILL = ["policy fifo", "enqueue -1", "enqueue 9", "drain", "drain", "limit 2"]
TEST2_EXIT_ENQUEUE = "enqueue 3"  # leaves the queue non-empty for the exit-time drain
TEST2_ONLY = [*TEST2_FILL, TEST2_EXIT_ENQUEUE]


@options
class _Options:
    pass


async def _ctl(host: UnixHost, command: str) -> str:
    """Write one command to the demo's control file and return the read-back line."""
    write = f"sh -c {shlex.quote(f'echo {shlex.quote(command)} > {CTL}')}"
    result = await host.run(write, sudo=True)
    if not result.is_ok:
        raise RuntimeError(f"{host.id}: `{command}` failed: {result.only.value}")
    read = await host.run(f"cat {CTL}", sudo=True)
    return read.only.value.strip()


async def _install(host: UnixHost) -> None:
    for product in host.products:  # declaration order: the library first
        if not await product.is_installed(host):
            result = await product.install(host)
            if not result.is_ok:
                raise RuntimeError(f"{host.id}: installing {product.name} failed: {result.msg}")


async def _uninstall(host: UnixHost) -> None:
    for product in reversed(host.products):  # the demo before the library it depends on
        result = await product.uninstall(host)
        if not result.is_ok:
            raise RuntimeError(f"{host.id}: uninstalling {product.name} failed: {result.msg}")


class TestKmodDemo(OttoSuite):
    Options = _Options

    @pytest_asyncio.fixture(autouse=True, scope="class")
    @classmethod
    async def _modules(cls):
        cls._hosts = list(all_hosts(_HOSTS))
        failed = {
            h: r
            for h, r in (await do_for_all_hosts(_install, pattern=_HOSTS)).items()
            if isinstance(r, BaseException)
        }
        if failed:
            raise RuntimeError(f"module install failed on: {failed}")

        async def run_common(host: UnixHost) -> str:
            last = ""
            for command in COMMON:
                last = await _ctl(host, command)
            return last

        # Run once per host, here rather than in a test: the ONLY mixes below
        # assume the queue state (and the "limit 4" cap) this leaves behind,
        # so it must precede both of them regardless of pytest-randomly's
        # chosen order for the test methods in this class.
        cls._common_lines = await do_for_all_hosts(run_common, pattern=_HOSTS)
        failed = {h: r for h, r in cls._common_lines.items() if isinstance(r, BaseException)}
        if failed:
            raise RuntimeError(f"common mix failed on: {failed}")
        yield
        failed = {
            h: r
            for h, r in (await do_for_all_hosts(_uninstall, pattern=_HOSTS)).items()
            if isinstance(r, BaseException)
        }
        if failed:
            raise RuntimeError(f"module uninstall failed on: {failed}")

    @pytest.mark.integration
    async def test_common_mix(self) -> None:
        for host_id, line in self._common_lines.items():
            assert "enqueued=6 dropped=1 drained=6 sum=15 err=0" in line, f"{host_id}: {line}"

    @pytest.mark.integration
    async def test_test1_mix_and_a_mid_suite_dump(self) -> None:
        host = next(h for h in self._hosts if h.id == "test1")
        for command in TEST1_FILL:
            await _ctl(host, command)
        assert "err=-22" in await _ctl(host, TEST1_ERRORS[0])  # bogus -> -EINVAL
        assert "err=-34" in await _ctl(host, TEST1_ERRORS[1])  # limit 999 -> -ERANGE
        await _ctl(host, TEST1_EXIT_ENQUEUE)
        dumped = await host.run(f"sh -c {shlex.quote(f'echo 1 > {DUMP}')}", sudo=True)
        assert dumped.is_ok, dumped.only.value

    @pytest.mark.integration
    async def test_test2_stop_marker_and_limit(self) -> None:
        host = next(h for h in self._hosts if h.id == "test2")
        line = ""
        for command in TEST2_FILL:
            line = await _ctl(host, command)
        assert "len=0 cap=2" in line, line
        assert "err=0" in line, line
        line = await _ctl(host, TEST2_EXIT_ENQUEUE)
        assert "len=1 cap=2" in line, line
        assert "err=0" in line, line
