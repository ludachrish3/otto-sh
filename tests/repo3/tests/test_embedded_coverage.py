"""Embedded (Zephyr LLEXT) coverage tests.

The embedded analogue of repo1's ``TestCoverageProduct``. Instead of compiling a
host binary and emitting ``.gcda`` to a filesystem, it loads a
coverage-instrumented LLEXT extension (``product/``) onto each embedded coverage
host, runs its operations over the console to exercise code paths, and lets
``otto test --cov``'s :class:`~otto.coverage.fetcher.embedded.EmbeddedGcdaCollector`
trigger ``cov_dump`` and decode the serial hexdump into ``.gcda``.

Run against the standard ``embedded`` lab; the coverage host(s) are selected by
the repo-declared ``[coverage].hosts`` regex (not a dedicated lab), e.g.::

    otto test --cov --lab embedded TestEmbeddedCoverage
    otto cov report <output_dir> --dir ./report

The extension is declared as a ``kind = "embedded"`` ``[[products]]`` entry (one
per Zephyr version, sharing one name), so the per-host lifecycle is the product
seam's: ``product.install`` (load + the ``call_after_load`` ``cov_init``) ->
``call_fn <op>`` (exercise) -> [collector: ``call_fn cov_dump``] ->
``product.uninstall`` (teardown — skipped under ``--cov`` so the extension is
still loaded when the collector dumps it).

The suite builds a version-matched product for each host (keyed by
``host.os_version``), reading from the per-version ``build_dir`` declared in the
optional ``[coverage.embedded].builds."<version>"`` table, falling back to the
single ``[coverage.embedded].build_dir`` when no per-version entry exists.
Each distinct ``(build_dir, zver)`` pair is built exactly once even when multiple
hosts share the same Zephyr version.
"""

import logging
import re
import struct
from pathlib import Path

import pytest
import pytest_asyncio

from otto.config import get_repos
from otto.config.fleet import all_hosts
from otto.coverage import clean_coverage
from otto.coverage.fetcher.embedded import decode_cov_dump
from otto.declared import DeclaredEntry
from otto.host import LocalHost
from otto.host.binary_loader import BinaryLoader
from otto.host.embedded_host import EmbeddedHost
from otto.host.embedded_kind import EmbeddedProduct
from otto.host.product import PRODUCT_KINDS
from otto.utils import Status

logger = logging.getLogger(__name__)

PRODUCT_DIR = Path(__file__).resolve().parent.parent / "product"
BUILD_SCRIPT = PRODUCT_DIR / "build.sh"


def _embedded_cov_config() -> dict:
    """Return the ``[coverage.embedded]`` table from the first repo declaring one."""
    for repo in get_repos():
        embedded = (repo.settings.get("coverage") or {}).get("embedded")
        if embedded:
            return embedded
    return {}


def _product_of(host: EmbeddedHost) -> EmbeddedProduct:
    """*host*'s declared LLEXT coverage product.

    The extension is a ``[[products]]`` entry now, not a ``[coverage.embedded]``
    key: the entry owns the name, the version-matched artifact, and the
    load/unload lifecycle, and that same name is the ``<product>`` segment of
    the run tree and the name the collector dumps through. Reading it off the
    host is what keeps the suite and the collector naming one thing.
    """
    products = [p for p in host.products if isinstance(p, EmbeddedProduct)]
    if len(products) != 1:
        raise RuntimeError(
            f"{host.id}: expected exactly one embedded product, got "
            f"{[p.name for p in products]} — check the [[products]] match tables"
        )
    return products[0]


def _extension(host: EmbeddedHost) -> str:
    return _product_of(host).name


def _extension_path_from(build_dir: str, ext: str) -> Path:
    """Path of the pre-built, stripped LLEXT extension for *build_dir* (what gets loaded)."""
    llext = Path(build_dir) / "zephyr" / f"{ext}.stripped.llext"
    if not llext.exists():
        raise RuntimeError(f"extension not built: {llext} — build product/ first (see its README)")
    return llext


def _build_dir_for(host: EmbeddedHost) -> str:
    """Resolve the ``build_dir`` for *host*'s Zephyr version.

    Looks up ``host.os_version`` in the optional
    ``[coverage.embedded].builds."<version>"`` table first; falls back to the
    single ``[coverage.embedded].build_dir``. Raises :exc:`RuntimeError` when
    neither is configured (mirrors the existing "build_dir is not configured"
    guard).
    """
    cfg = _embedded_cov_config()
    if host.os_version:
        per_version = cfg.get("builds", {}).get(host.os_version, {})
        if per_version.get("build_dir"):
            return per_version["build_dir"]
    build_dir = cfg.get("build_dir")
    if not build_dir:
        raise RuntimeError("[coverage.embedded].build_dir is not configured")
    return build_dir


def _zver_for(host: EmbeddedHost) -> "str | None":
    """Map *host*'s ``os_version`` to ``build.sh``'s ``zver`` positional argument.

    Returns ``"v" + os_version.replace(".", "_")`` (e.g. ``"3.7"`` → ``"v3_7"``,
    ``"4.4"`` → ``"v4_4"``). Returns ``None`` when ``os_version`` is falsy so the
    caller can omit the argument and rely on ``build.sh``'s default ``v3_7``.
    """
    if not host.os_version:
        return None
    return "v" + host.os_version.replace(".", "_")


async def _build_extension_for(build_dir: str, zver: "str | None") -> None:
    """Rebuild the LLEXT coverage extension into *build_dir* for Zephyr *zver*.

    The embedded analogue of repo1's ``_compile_product``: the suite keeps the
    product up to date rather than trusting a stale pre-built artifact. Runs
    ``product/build.sh {build_dir} [{zver}]`` on the machine executing the suite
    (the dev VM, where ``build_dir`` lives) and hard-fails if the build can't run
    or errors. The script is idempotent, so a pre-existing build dir is fine.
    When *zver* is ``None`` the argument is omitted and ``build.sh``'s default
    (``v3_7``) is used.
    """
    cmd = f"bash {BUILD_SCRIPT} {build_dir}"
    if zver is not None:
        cmd = f"{cmd} {zver}"
    localhost = LocalHost()
    try:
        result = await localhost.exec(cmd, timeout=900)
        if result.status != Status.Success:
            raise RuntimeError(f"extension build failed (see {BUILD_SCRIPT}):\n{result.value}")
        logger.info("Rebuilt the coverage extension into %s (zver=%s)", build_dir, zver)
    finally:
        await localhost.close()


def _coverage_host_pattern() -> "re.Pattern[str] | None":
    """Return the repo-declared ``[coverage].hosts`` selector, compiled (or ``None``)."""
    for repo in get_repos():
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
    ext = _extension(host)
    result = await host.exec(f"llext call_fn {ext} {fn}", timeout=timeout)
    if result.status != Status.Success:
        raise RuntimeError(f"call_fn {fn} failed on {host.id}: {result.value}")


async def _install_everywhere() -> list[EmbeddedHost]:
    """Rebuild (per version), then load + initialise the extension on every
    embedded coverage host, returning those hosts.

    Each distinct ``(build_dir, zver)`` pair is built exactly once so that
    multiple hosts sharing the same Zephyr version do not trigger redundant
    rebuilds. Each host is then loaded its own version-matched artifact.
    """
    hosts = _embedded_hosts()
    if not hosts:
        pytest.skip("no embedded coverage hosts in the active lab")

    # Keep each version's product up to date — repo1's TestCoverageProduct
    # compiles its binary the same way. Build before reading the artifact
    # below so the loaded extension always reflects the current source.
    # Cache by (build_dir, zver) so same-version hosts share one build.
    built: set[tuple[str, "str | None"]] = set()
    host_build_dir: dict[str, str] = {}
    for host in hosts:
        build_dir = _build_dir_for(host)
        zver = _zver_for(host)
        if (build_dir, zver) not in built:
            await _build_extension_for(build_dir, zver)
            built.add((build_dir, zver))
        host_build_dir[host.id] = build_dir
        # Two declarations have to agree for this bed to mean anything: the
        # [[products]] artifact (what gets LOADED) and [coverage.embedded]'s
        # per-version build_dir (whose .gcno DECODES the dump). Disagreement
        # is not a load failure — it surfaces much later as gcov's stamp
        # mismatch — so it is checked here, where the fix is obvious.
        product = _product_of(host)
        expected = _extension_path_from(build_dir, product.name)
        if product.artifact != expected:
            raise RuntimeError(
                f"{host.id}: [[products]] artifact {product.artifact} is not the "
                f"{host.os_version} build's {expected} — the loaded extension and the "
                ".gcno that decodes its .gcda would come from different builds"
            )

    for host in hosts:
        product = _product_of(host)
        # Evict any resident copy first so install loads the freshly-built
        # bytes: otherwise llext_load refcount-bumps the stale build, the
        # rebuilt .gcno's new stamp no longer matches the dumped .gcda, and
        # `otto cov report` fails with a stamp mismatch. The product's
        # uninstall drains the LLEXT use-count to 0 (idempotent when
        # nothing is loaded).
        await product.uninstall(host)
        # install = load + every `call_after_load` entry, i.e. the gcov
        # constructor that gives cov_dump a registered gcov_info.
        install = await product.install(host)
        if not install.is_ok:
            raise RuntimeError(f"install did not load {product.name} on {host.id}: {install.msg}")
        logger.info("Loaded %s (%s) on %s", product.name, host_build_dir[host.id], host.id)
    return hosts


async def _unload_unless_collecting(hosts: list[EmbeddedHost], cov_active: bool) -> None:
    """Unload the extension from *hosts* — unless ``--cov`` still has to dump it."""
    if cov_active:
        return
    for host in hosts:
        product = _product_of(host)
        await product.uninstall(host)
        logger.info("Unloaded %s from %s", product.name, host.id)


_GCOV_TAG_FUNCTION = 0x01000000
_GCOV_TAG_COUNTER_BASE = 0x01A10000
_GCOV_COUNTER_KINDS = 9


def _is_counter_tag(tag: int) -> bool:
    """Answer whether *tag* opens a counter record (``GCOV_TAG_FOR_COUNTER``)."""
    offset = tag - _GCOV_TAG_COUNTER_BASE
    return offset >= 0 and offset % (1 << 17) == 0 and offset >> 17 < _GCOV_COUNTER_KINDS


def _function_counters(gcda: bytes) -> dict[int, list[int]]:
    """Each function record's counters in an embedded-gcov ``.gcda``, by function ident.

    The layout ``gcov_convert_to_gcda`` writes: a header (magic, version,
    stamp, plus a checksum word from gcc 12), then per function a function
    record (ident, two checksums) followed by its counter records, each
    counter a little-endian 64-bit value. gcc 12 counts record lengths in
    bytes and earlier gccs in 32-bit words; the header length says which, as
    the first record after it is always a function record.
    """
    layouts = [(16, 1), (12, 4)]  # (header bytes, bytes per length unit): gcc >= 12, older
    fits = [
        (header_len, unit)
        for header_len, unit in layouts
        if len(gcda) >= header_len + 4
        and struct.unpack_from("<I", gcda, header_len)[0] == _GCOV_TAG_FUNCTION
    ]
    if not fits:
        raise AssertionError(f"not an embedded-gcov .gcda: {gcda[:20].hex()}")
    header_len, unit = fits[0]
    counters: dict[int, list[int]] = {}
    ident = None
    pos = header_len
    while pos + 8 <= len(gcda):
        tag, length = struct.unpack_from("<II", gcda, pos)
        pos += 8
        nbytes = length * unit
        if tag == _GCOV_TAG_FUNCTION:
            ident = struct.unpack_from("<I", gcda, pos)[0]
            counters[ident] = []
        elif ident is not None and _is_counter_tag(tag):
            counters[ident].extend(struct.unpack_from(f"<{nbytes // 8}Q", gcda, pos))
        pos += nbytes
    return counters


def _entry_like(
    product: EmbeddedProduct, *, name: str | None = None, **params: str
) -> DeclaredEntry:
    """A ``[[products]]`` entry declaring *product*'s artifact, with *params* on top."""
    return DeclaredEntry(
        name=name or product.name,
        kind="embedded",
        seam="products",
        owner=None,
        base_dir=PRODUCT_DIR,
        params={"artifact": str(product.artifact), **params},
    )


def _loader_of(host: EmbeddedHost) -> BinaryLoader:
    """*host*'s binary loader — every coverage board in this lab declares one."""
    if host.loader is None:
        raise RuntimeError(f"{host.id} has no binary loader")
    return host.loader


async def _dump_counters(host: EmbeddedHost) -> dict[tuple[str, int], list[int]]:
    """Dump *host*'s product counters the way the collector does.

    Keyed by ``(.gcda name, function ident)``: the idents are only unique
    within one translation unit's ``.gcda``.
    """
    product = _product_of(host)
    command = _loader_of(host).call_command(product.name, product.dump_fn)
    result = await host.exec(command, timeout=120)
    if result.status != Status.Success:
        raise RuntimeError(f"{command} failed on {host.id}: {result.value}")
    blocks = decode_cov_dump(result.value)
    assert blocks, f"{command} on {host.id} decoded no .gcda:\n{result.value[-2000:]}"
    return {
        (name, ident): values
        for name, gcda in sorted(blocks.items())
        for ident, values in _function_counters(gcda).items()
    }


class TestEmbeddedCoverage:
    """Exercise the LLEXT coverage product over the console on each embedded
    coverage host, leaving the extension loaded for ``--cov`` collection.
    """

    @pytest_asyncio.fixture(autouse=True, scope="class")
    @classmethod
    async def _load_extension(cls, ctx):
        """Load + initialise the extension on every embedded host; unload on
        teardown (unless ``--cov`` needs it kept for the post-test dump). A
        classmethod on the run's session loop (no ``loop_scope`` pin).
        """
        cls._hosts = await _install_everywhere()
        yield
        await _unload_unless_collecting(cls._hosts, ctx.cov)

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


_PRODUCT_OPS = ["op_clamp_lo", "op_clamp_in", "op_div_ok", "op_div_zero"]
"""Every exported operation — together they run all six product functions
(``math_clamp``, ``math_div`` and the four ``op_*``)."""


class TestEmbeddedCounterReset:
    """``otto cov clean`` zeroes a board's counters in place, an unexported
    ``reset_fn`` is a failure rather than a silent no-op, and a board without
    the extension has nothing to clear.

    Its own class, not part of :class:`TestEmbeddedCoverage`: a clean there
    would zero the very counters ``--cov`` collects after that class, so the
    two run as separate ``otto test`` invocations. Run under ``--cov``, the
    collection that follows this class dumps the post-clean counters, which
    is what lets the embedded end-to-end test read them back by function name
    through the real report pipeline.
    """

    @pytest_asyncio.fixture(autouse=True, scope="class")
    @classmethod
    async def _load_extension(cls, ctx):
        """Load + initialise the extension everywhere, as :class:`TestEmbeddedCoverage` does."""
        cls._hosts = await _install_everywhere()
        yield
        await _unload_unless_collecting(cls._hosts, ctx.cov)

    @pytest.mark.integration
    async def test_clean_zeroes_the_counters(self) -> None:
        """Exercise every operation, clean, dump: what the exercise counted is zero again.

        Not every counter in the dump is zero, and cannot be: embedded-gcov is
        compiled into the same instrumented translation unit, so the reset's
        own tail and the dump that reads the counters back tick the runtime's
        counters as they run. The product's six functions are the ones the
        exercise raised and nothing after the clean runs, so at least six
        functions have to go from counted to all-zero. The end-to-end test
        names them through the post-clean ``--cov`` report.
        """
        for host in self._hosts:
            product = _product_of(host)
            for op in _PRODUCT_OPS:
                await _call(host, op)
            before = await _dump_counters(host)

            report = await clean_coverage(host_ids=[host.id])

            assert report.ok, f"clean failed on {host.id}: {report.failed}"
            assert (host.id, product.name) in report.cleared, report.hosts
            after = await _dump_counters(host)
            assert after.keys() == before.keys(), "the dump's function records changed"
            zeroed = [key for key, values in before.items() if any(values) and not any(after[key])]
            product_functions = len(_PRODUCT_OPS) + 2  # the ops, math_clamp, math_div
            assert len(zeroed) >= product_functions, (
                f"{host.id}: only {len(zeroed)} function(s) went from counted to zero "
                f"after the clean; before={before} after={after}"
            )

    @pytest.mark.integration
    async def test_unexported_reset_fn_fails(self) -> None:
        """A ``reset_fn`` the loaded extension does not export is a failed reset.

        Built through the ``embedded`` kind itself, so the declaration is
        parsed the way a ``[[products]]`` entry naming ``reset_fn`` would be.

        What the boards answer, recorded on zephyr37-llext (3.7) and
        zephyr44-llext (4.4): ``llext call_fn cov_ext no_such_fn`` prints
        nothing and ``retval`` reads 0 — the shell's ``cmd_llext_call_fn``
        ignores ``llext_call_fn``'s ``-ENOENT`` — while the real ``cov_reset``
        prints embedded-gcov's ``gcov_clear``. So the kind fails a success the
        board did not confirm; this pins that the board's real answer is one.
        """
        for host in self._hosts:
            product = _product_of(host)
            missing = PRODUCT_KINDS.get("embedded")(
                _entry_like(product, reset_fn="no_such_fn"), host
            )
            call = _loader_of(host).call_command(product.name, "no_such_fn")
            raw = await host.exec(call, timeout=60)
            assert raw.status is Status.Success, f"{host.id}: {call} -> {raw.status}"
            assert not raw.value.strip(), f"{host.id}: {call} printed {raw.value!r}"

            result = await missing.reset_coverage(host)

            assert result.status is Status.Error, f"{host.id}: reset reported {result.status}"
            assert "reset_fn 'no_such_fn' failed" in result.msg
            assert "silent success" in result.msg

    @pytest.mark.integration
    async def test_an_extension_that_is_not_loaded_has_nothing_to_clear(self) -> None:
        """A board without the extension answers the reset with a successful no-op.

        ``llext call_fn <absent> cov_reset`` prints ``No such extension
        <absent>`` on both boards; the loader recognises that line, so a
        clean of a board that never loaded the product (a pre-run clean
        before the suite installs it) is not a failure.
        """
        for host in self._hosts:
            product = _product_of(host)
            absent = PRODUCT_KINDS.get("embedded")(
                _entry_like(product, name=f"{product.name}_absent"), host
            )
            call = _loader_of(host).call_command(absent.name, absent.reset_fn)
            raw = await host.exec(call, timeout=60)
            assert f"No such extension {absent.name}" in raw.value, (
                f"{host.id}: {call} -> {raw.status} {raw.value!r}"
            )

            result = await absent.reset_coverage(host)

            assert result.status is Status.Success, f"{host.id}: {result.status} {result.msg}"
            assert result.msg == "not loaded, so there are no counters to clear"
