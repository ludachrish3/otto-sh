"""Per-file transfer mapping semantics (spec 2026-07-01)."""

from pathlib import Path

from otto.host.transfer.base import aggregate_transfer
from otto.result import Result
from otto.utils import Status


def test_all_ok_aggregate():
    per_file = {
        Path("a"): Result(Status.Success, value=Path("/dst/a")),
        Path("b"): Result(Status.Success, value=Path("/dst/b")),
    }
    agg = aggregate_transfer(per_file)
    assert agg.is_ok
    assert agg.value is per_file
    assert agg.msg == ""


def test_failure_aggregate_is_first_non_ok_with_msg():
    per_file = {
        Path("a"): Result(Status.Success, value=Path("/dst/a")),
        Path("b"): Result(Status.Error, msg="b: connection reset"),
        Path("c"): Result(Status.Skipped, msg="not attempted"),
    }
    agg = aggregate_transfer(per_file)
    assert agg.status is Status.Error
    assert "b: connection reset" in agg.msg
    assert agg.value[Path("c")].status is Status.Skipped


def test_trailing_skipped_alone_never_fails_aggregate():
    per_file = {Path("a"): Result(Status.Skipped, msg="not attempted")}
    assert aggregate_transfer(per_file).is_ok
