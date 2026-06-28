"""Unit tests for the standalone ExpectCollector (the conformance engine)."""

import logging

import pytest

from otto.suite.expect import ExpectCollector, expect


class TestExpectCollector:
    def test_passing_expect_records_nothing(self):
        c = ExpectCollector()
        c.expect(1 == 1)
        c.expect(True, "should not record")
        assert c.failures == []

    def test_failing_expect_records_report(self):
        c = ExpectCollector()
        x = 42
        c.expect(x == 99, "math is broken")
        assert len(c.failures) == 1
        report = c.failures[0]
        assert "math is broken" in report
        assert "x = 42" in report  # locals captured

    def test_multiple_failures_accumulate_in_order(self):
        c = ExpectCollector()
        c.expect(False, "first")
        c.expect(False, "second")
        assert len(c.failures) == 2
        assert "first" in c.failures[0]
        assert "second" in c.failures[1]

    def test_reset_clears_failures(self):
        c = ExpectCollector()
        c.expect(False, "boom")
        assert c.failures
        c.reset()
        assert c.failures == []

    def test_raise_if_failures_raises_with_aggregate_report(self):
        c = ExpectCollector()
        c.expect(False, "alpha")
        c.expect(False, "beta")
        with pytest.raises(AssertionError) as exc:
            c.raise_if_failures()
        msg = str(exc.value)
        assert "2 expectation(s) failed" in msg
        assert "alpha" in msg
        assert "beta" in msg

    def test_raise_if_failures_no_raise_when_clean(self):
        c = ExpectCollector()
        c.expect(True)
        c.raise_if_failures()  # must not raise

    def test_logger_warns_on_failure(self, caplog):
        logger = logging.getLogger("otto.test.expect")
        c = ExpectCollector(logger=logger)
        with caplog.at_level(logging.WARNING, logger="otto.test.expect"):
            c.expect(False, "logged failure")
        assert any("logged failure" in r.message for r in caplog.records)

    def test_module_level_expect_uses_explicit_collector(self):
        c = ExpectCollector()
        expect(2 + 2 == 5, "via module fn", collector=c)
        assert len(c.failures) == 1
        assert "via module fn" in c.failures[0]
