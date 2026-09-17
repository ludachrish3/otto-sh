"""The llext product kind: an embedded extension modelled as a product."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from otto.declared import DeclaredEntry
from otto.host import product as product_mod
from otto.host.binary_loader import LlextHexLoader
from otto.host.llext_kind import LlextProduct
from otto.result import CommandResult, Result
from otto.utils import Status


class _Board:
    """Embedded-host double: load/unload/exec recorded, loader real."""

    def __init__(
        self,
        *,
        list_output: str = "",
        list_fails: bool = False,
        load_fails: bool = False,
        fn_fails: str = "",
    ):
        self.id = "board1"
        self.loader = LlextHexLoader()
        self.calls: list[tuple] = []
        self.list_output = list_output
        self.list_fails = list_fails
        self.load_fails = load_fails
        self.fn_fails = fn_fails

    async def load(self, file, name, **kw):
        self.calls.append(("load", file, name))
        if self.load_fails:
            return Result(Status.Error, msg="Failed to load: return code -8")
        return Result(Status.Success)

    async def unload(self, name, **kw):
        self.calls.append(("unload", name))
        return Result(Status.Success)

    async def exec(self, cmd, timeout=None):
        self.calls.append(("exec", cmd))
        if self.fn_fails and cmd.endswith(f" {self.fn_fails}"):
            return CommandResult(Status.Error, value="undefined symbol", command=cmd, retcode=1)
        if cmd == "llext list":
            status = Status.Error if self.list_fails else Status.Success
            return CommandResult(status, value=self.list_output, command=cmd, retcode=0)
        return CommandResult(Status.Success, value="", command=cmd, retcode=0)


def _entry(**params):
    return DeclaredEntry(
        name="cov_ext",
        kind="llext",
        seam="products",
        owner="repo3",
        base_dir=Path("/repo"),
        match={},
        params={"artifact": "build/cov_ext.llext", **params},
    )


def _build(host, **params) -> LlextProduct:
    return product_mod.PRODUCT_KINDS.get("llext")(_entry(**params), host)


def test_kind_is_registered_for_products_only():
    from otto.host.dev_tool import DEV_TOOL_KINDS

    assert "llext" in product_mod.PRODUCT_KINDS
    assert "llext" not in DEV_TOOL_KINDS


def test_factory_refuses_a_host_without_a_loader():
    with pytest.raises(ValueError, match=r"cov_ext.*no binary loader"):
        _build(SimpleNamespace(id="h1", loader=None))


@pytest.mark.asyncio
async def test_install_loads_then_calls_each_after_load_fn():
    board = _Board()
    p = _build(board, call_after_load=["cov_init", "warm"])
    assert (await p.install(board)).is_ok
    assert board.calls == [
        ("load", Path("/repo/build/cov_ext.llext"), "cov_ext"),
        ("exec", "llext call_fn cov_ext cov_init"),
        ("exec", "llext call_fn cov_ext warm"),
    ]


@pytest.mark.asyncio
async def test_install_returns_the_load_failure_and_calls_nothing_after_it():
    board = _Board(load_fails=True)
    result = await _build(board, call_after_load=["cov_init"]).install(board)
    assert not result.is_ok
    assert "return code -8" in result.msg  # the loader's own failure text, unwrapped
    assert board.calls == [("load", Path("/repo/build/cov_ext.llext"), "cov_ext")]


@pytest.mark.asyncio
async def test_install_stops_at_the_first_failing_fn_and_names_it():
    board = _Board(fn_fails="cov_init")
    result = await _build(board, call_after_load=["cov_init", "warm"]).install(board)
    assert not result.is_ok
    assert "cov_init" in result.msg
    assert "warm" not in result.msg  # stopped before it — a half-init is not an install
    assert ("exec", "llext call_fn cov_ext warm") not in board.calls


@pytest.mark.asyncio
async def test_install_refuses_a_host_that_lost_its_loader_before_touching_it():
    # The factory refuses a loaderless host at build; install is handed a host
    # again at call time, so it must not silently no-op on one without a loader
    # — and must refuse BEFORE the load, naming the host, not its class.
    board = _Board()
    product = _build(board)
    board.loader = None
    with pytest.raises(ValueError, match=r"board1 has no binary loader"):
        await product.install(board)
    assert board.calls == []


@pytest.mark.asyncio
async def test_stage_is_a_noop_and_uninstall_unloads():
    board = _Board()
    p = _build(board)
    assert (await p.stage(board)).status is Status.Success
    assert (await p.uninstall(board)).is_ok
    assert board.calls == [("unload", "cov_ext")]


@pytest.mark.asyncio
async def test_is_installed_asks_the_loader_list():
    resident = _Board(list_output="cov_ext\n")
    assert await _build(resident).is_installed(resident)
    absent = _Board(list_output="other\n")
    assert not await _build(absent).is_installed(absent)


@pytest.mark.asyncio
async def test_is_installed_is_false_when_the_list_command_itself_failed():
    # A wedged console's capture can still hold the name (the echo of the
    # earlier load_hex, or a stale listing). Matching it would skip the
    # install, so call_after_load never runs and coverage comes back empty.
    board = _Board(list_output="cov_ext\n", list_fails=True)
    assert not await _build(board).is_installed(board)


def test_dump_fn_defaults_and_overrides():
    assert _build(_Board()).dump_fn == "cov_dump"
    assert _build(_Board(), dump_fn="gcov_flush").dump_fn == "gcov_flush"


def test_empty_dump_fn_is_refused_rather_than_silently_defaulted():
    with pytest.raises(ValueError, match=r"'cov_ext'.*'dump_fn' must not be empty"):
        _build(_Board(), dump_fn="")


def test_instrumented_scans_the_llext_object(tmp_path):
    art = tmp_path / "cov_ext.llext"
    art.write_bytes(b"\x7fELF\0/home/x/cov_ext.c.gcda\0")
    p = product_mod.PRODUCT_KINDS.get("llext")(
        DeclaredEntry(
            name="cov_ext",
            kind="llext",
            seam="products",
            owner="r",
            base_dir=tmp_path,
            match={},
            params={"artifact": "cov_ext.llext"},
        ),
        _Board(),
    )
    assert p.instrumented() is True


def test_unknown_param_names_the_valid_list():
    with pytest.raises(
        ValueError,
        match=r"valid: artifact, call_after_load, dump_fn, instrumented, debug_log_globs",
    ):
        _build(_Board(), bogus=1)
