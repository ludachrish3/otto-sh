"""Unit tests for the otto.result family (spec 2026-07-01)."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from otto.result import CommandResult, Result, Results
from otto.utils import Status


class TestResult:
    def test_defaults(self):
        r = Result(Status.Success)
        assert r.value is None
        assert r.msg == ""
        assert r.is_ok
        assert bool(r)

    def test_bool_is_status_not_value(self):
        assert not Result(Status.Failed, value=[1, 2, 3])
        assert Result(Status.Success, value=[])

    def test_frozen(self):
        with pytest.raises(FrozenInstanceError):
            Result(Status.Success).status = Status.Failed  # type: ignore[misc]

    @pytest.mark.parametrize(
        ("status", "code"),
        [
            (Status.Success, 0),
            (Status.Skipped, 0),  # is_ok -> 0, never 4
            (Status.Failed, 1),
            (Status.Error, 2),
            (Status.Unstable, 3),
        ],
    )
    def test_exit_code_status_mapping(self, status, code):
        assert Result(status).exit_code == code

    def test_value_can_hold_transfer_mapping(self):
        per_file = {Path("a.bin"): Result(Status.Success, value=Path("/dst/a.bin"))}
        r = Result(Status.Success, value=per_file)
        assert r.value[Path("a.bin")].value == Path("/dst/a.bin")


class TestCommandResult:
    def test_fields(self):
        cr = CommandResult(Status.Success, value="hi", command="echo hi", retcode=0)
        assert cr.value == "hi"
        assert cr.command == "echo hi"
        assert cr.retcode == 0
        assert cr.exit_code == 0

    def test_exit_code_is_retcode_when_failed(self):
        assert CommandResult(Status.Failed, command="x", retcode=42).exit_code == 42

    def test_exit_code_never_ran_is_255(self):
        assert CommandResult(Status.Error, command="x", retcode=-1).exit_code == 255

    def test_exit_code_failed_with_retcode_zero_falls_back_to_status(self):
        # e.g. expect mismatch: command exited 0 but otto marked it Failed
        assert CommandResult(Status.Failed, command="x", retcode=0).exit_code == 1

    def test_is_a_result(self):
        assert isinstance(CommandResult(Status.Success), Result)


def _cr(status: Status, retcode: int = 0, command: str = "c") -> CommandResult:
    return CommandResult(status, value="", command=command, retcode=retcode)


class TestResults:
    def test_collect_all_ok(self):
        res = Results.collect([_cr(Status.Success), _cr(Status.Skipped)])
        assert res.status is Status.Success
        assert res.is_ok

    def test_collect_aggregate_is_first_non_ok(self):
        res = Results.collect([_cr(Status.Success), _cr(Status.Error, 5), _cr(Status.Failed, 1)])
        assert res.status is Status.Error

    def test_collect_empty_is_success(self):
        res = Results.collect([])
        assert res.status is Status.Success
        assert len(res) == 0

    def test_sequence_behavior(self):
        items = [_cr(Status.Success, command="a"), _cr(Status.Success, command="b")]
        res = Results.collect(items)
        assert len(res) == 2
        assert res[0].command == "a"
        assert [c.command for c in res] == ["a", "b"]
        assert res[0:2] == items  # slice returns a plain list

    def test_bool_is_status_not_emptiness(self):
        assert Results.collect([])  # empty but ok -> truthy
        assert not Results.collect([_cr(Status.Failed, 1)])

    def test_only(self):
        assert Results.collect([_cr(Status.Success, command="a")]).only.command == "a"

    @pytest.mark.parametrize("n", [0, 2])
    def test_only_raises_unless_exactly_one(self, n):
        with pytest.raises(ValueError, match="exactly 1"):
            _ = Results.collect([_cr(Status.Success)] * n).only

    def test_first_failure(self):
        res = Results.collect([_cr(Status.Success), _cr(Status.Failed, 7)])
        assert res.first_failure is not None
        assert res.first_failure.retcode == 7
        assert Results.collect([_cr(Status.Success)]).first_failure is None

    def test_exit_code_delegates_to_first_failure(self):
        res = Results.collect([_cr(Status.Success), _cr(Status.Failed, 42)])
        assert res.exit_code == 42
        assert Results.collect([_cr(Status.Success)]).exit_code == 0

    def test_is_a_result(self):
        assert isinstance(Results.collect([]), Result)


def test_top_level_lazy_exports():
    import otto

    assert otto.Result is Result
    assert otto.CommandResult is CommandResult
    assert otto.Results is Results
