"""The embedded product kind: an embedded extension modelled as a product."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from otto.declared import DeclaredEntry
from otto.host import product as product_mod
from otto.host.binary_loader import LlextHexLoader
from otto.host.embedded_kind import EmbeddedProduct
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
        exec_result: CommandResult | None = None,
    ):
        self.id = "board1"
        self.loader = LlextHexLoader()
        self.calls: list[tuple] = []
        self.list_output = list_output
        self.list_fails = list_fails
        self.load_fails = load_fails
        self.fn_fails = fn_fails
        self.exec_result = exec_result

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
        if self.exec_result is not None:
            return self.exec_result
        return CommandResult(Status.Success, value="", command=cmd, retcode=0)


def _entry(*, kind="embedded", **params):
    return DeclaredEntry(
        name="cov_ext",
        kind=kind,
        seam="products",
        owner="repo3",
        base_dir=Path("/repo"),
        match={},
        params={"artifact": "build/cov_ext.llext", **params},
    )


def _host():
    return SimpleNamespace(id="h1", loader=None)


def _build(host, **params) -> EmbeddedProduct:
    return product_mod.PRODUCT_KINDS.get("embedded")(_entry(**params), host)


def test_kind_is_registered_for_products_only():
    from otto.host.dev_tool import DEV_TOOL_KINDS

    assert "embedded" in product_mod.PRODUCT_KINDS
    assert "embedded" not in DEV_TOOL_KINDS


def test_factory_refuses_a_host_without_a_loader():
    with pytest.raises(ValueError, match=r"cov_ext.*no binary loader"):
        _build(SimpleNamespace(id="h1", loader=None))


def test_the_old_kind_name_is_refused_naming_the_new_one():
    """``kind = "llext"`` is not silently accepted: the message names the rename.

    Through the real ingest path (:meth:`KindRegistry.build`), the same way
    the retired ``file``/``shell`` rename is proven in
    ``test_build_refuses_the_retired_file_kind_naming_shell`` — the retired
    name is refused before a factory is even looked up.
    """
    entry = _entry(kind="llext", artifact="x.llext")
    with pytest.raises(ValueError, match=r"'cov_ext': kind 'llext' is now 'embedded'"):
        product_mod.PRODUCT_KINDS.build([entry], _host())


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


def test_instrumented_scans_the_embedded_object(tmp_path):
    art = tmp_path / "cov_ext.llext"
    art.write_bytes(b"\x7fELF\0/home/x/cov_ext.c.gcda\0")
    p = product_mod.PRODUCT_KINDS.get("embedded")(
        DeclaredEntry(
            name="cov_ext",
            kind="embedded",
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
        match=(
            r"valid: artifact, call_after_load, dump_fn, reset_fn, instrumented, "
            r"debug_log_globs"
        ),
    ):
        _build(_Board(), bogus=1)


def test_an_extension_stages_no_artifact_and_takes_no_stage_dir():
    """The load IS the transfer: no destination to name, nothing to collide (issue #368)."""
    board = _Board()
    assert _build(board).stages_artifact is False
    with pytest.raises(ValueError, match=r"(?s)unknown param.*stage_dir"):
        _build(board, stage_dir="/opt")


def test_embedded_refuses_the_retired_dest_dir_key_with_the_same_rename_hint():
    """Every kind answers the rename the same way, even one with no stage_dir."""
    with pytest.raises(ValueError, match=r"(?s)'dest_dir'.*'stage_dir'"):
        _build(_Board(), dest_dir="/opt")


def test_reset_fn_defaults_to_cov_reset_and_is_validated_like_dump_fn():
    assert _build(_Board()).reset_fn == "cov_reset"
    assert _build(_Board(), reset_fn="zero").reset_fn == "zero"
    with pytest.raises(ValueError, match=r"'cov_ext'.*'reset_fn' must not be empty"):
        _build(_Board(), reset_fn="")


_BOARD_CONFIRMED = "gcov_clear"
"""What ``llext call_fn cov_ext cov_reset`` printed on the zephyr37-llext and
zephyr44-llext beds: embedded-gcov's ``__gcov_clear`` status line."""

_BOARD_NOT_LOADED = "No such extension cov_ext"
"""What the same call printed on both beds with no ``cov_ext`` resident."""


def _answer(status: Status, value: str) -> CommandResult:
    return CommandResult(
        status, value=value, command="", retcode=0 if status is Status.Success else 1
    )


@pytest.mark.asyncio
async def test_reset_calls_the_reset_fn_through_the_hosts_loader():
    board = _Board(exec_result=_answer(Status.Success, _BOARD_CONFIRMED))
    result = await _build(board).reset_coverage(board)
    assert result.status is Status.Success
    assert ("exec", "llext call_fn cov_ext cov_reset") in board.calls


@pytest.mark.asyncio
async def test_a_success_the_board_did_not_confirm_fails_naming_both_causes():
    """An unexported reset_fn: the shell's answer on both beds was an empty success."""
    board = _Board(exec_result=_answer(Status.Success, ""))
    result = await _build(board, reset_fn="no_such_fn").reset_coverage(board)
    assert result.status is Status.Error
    # Every consumer prefixes `<host>/<product>: ` itself; the reason never repeats the name.
    assert result.msg.startswith(
        "reset_fn 'no_such_fn' failed: the board did not confirm the reset (it printed nothing)."
    )
    assert "does not export 'no_such_fn'" in result.msg
    assert "silent success" in result.msg
    assert "GCOV_OPT_PRINT_STATUS" in result.msg
    assert "void cov_reset(void) { __gcov_clear(); }" in result.msg


@pytest.mark.asyncio
async def test_an_unconfirmed_reset_quotes_what_the_board_did_print():
    board = _Board(exec_result=_answer(Status.Success, "retval: 0"))
    result = await _build(board).reset_coverage(board)
    assert result.status is Status.Error
    assert result.msg.startswith(
        "reset_fn 'cov_reset' failed: the board did not confirm the reset "
        "(no 'gcov_clear' line in 'retval: 0')."
    )


@pytest.mark.asyncio
async def test_the_confirmation_must_be_its_own_word():
    board = _Board(exec_result=_answer(Status.Success, "gcov_clear_counters"))
    result = await _build(board).reset_coverage(board)
    assert result.status is Status.Error


@pytest.mark.asyncio
async def test_the_confirmation_must_be_its_own_line():
    """A reset_fn named ``gcov_clear``: the echoed call must not confirm itself."""
    board = _Board(exec_result=_answer(Status.Success, "llext call_fn cov_ext gcov_clear\n"))
    result = await _build(board, reset_fn="gcov_clear").reset_coverage(board)
    assert result.status is Status.Error
    assert "did not confirm the reset" in result.msg


@pytest.mark.asyncio
async def test_the_confirmation_line_may_carry_surrounding_whitespace():
    board = _Board(exec_result=_answer(Status.Success, "  gcov_clear\r\nretval: 0\r\n"))
    result = await _build(board).reset_coverage(board)
    assert result.status is Status.Success


@pytest.mark.parametrize("status", [Status.Error, Status.Failed, Status.Success])
@pytest.mark.asyncio
async def test_an_extension_that_is_not_loaded_has_nothing_to_clear(status):
    """The board's not-loaded line is a successful no-op, whatever status the frame gave it."""
    board = _Board(exec_result=_answer(status, _BOARD_NOT_LOADED))
    result = await _build(board).reset_coverage(board)
    assert result.status is Status.Success
    assert result.msg == "not loaded, so there are no counters to clear"


@pytest.mark.asyncio
async def test_another_extensions_absence_is_still_a_failure():
    board = _Board(exec_result=_answer(Status.Error, "No such extension cov_ext_two"))
    result = await _build(board).reset_coverage(board)
    assert result.status is Status.Error


@pytest.mark.asyncio
async def test_a_failed_reset_names_the_fix():
    board = _Board(
        exec_result=CommandResult(Status.Error, value="symbol not found", command="", retcode=1)
    )
    result = await _build(board).reset_coverage(board)
    assert result.status is Status.Error
    assert result.msg.startswith("reset_fn 'cov_reset' failed: symbol not found. An extension")
    assert "void cov_reset(void) { __gcov_clear(); }" in result.msg


@pytest.mark.parametrize("status", [Status.Error, Status.Failed])
@pytest.mark.asyncio
async def test_a_failed_reset_with_no_output_says_the_board_printed_nothing(status):
    board = _Board(exec_result=_answer(status, ""))
    result = await _build(board).reset_coverage(board)
    assert result.status is Status.Error
    assert result.msg.startswith(
        "reset_fn 'cov_reset' failed and the board printed nothing. An extension"
    )


@pytest.mark.asyncio
async def test_a_declined_reset_is_returned_as_declined():
    board = _Board(exec_result=CommandResult(Status.NotRun, value="", command="", retcode=0))
    result = await _build(board).reset_coverage(board)
    assert result.status is Status.NotRun
