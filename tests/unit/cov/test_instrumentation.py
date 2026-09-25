"""Instrumentation detection and the --cov decision table (spec §8)."""

import logging
from types import SimpleNamespace

import pytest

from otto.config.coverage_settings import CoverageConfigError
from otto.coverage.errors import CoverageNotInstrumentedError
from otto.coverage.instrumentation import (
    InstrumentationReport,
    InstrumentationRow,
    decide_coverage,
    detect,
    detect_for_lab,
    instrumented_products,
)


def _product(name, verdict):
    return SimpleNamespace(name=name, instrumented=lambda: verdict)


def _host(host_id, *products):
    return SimpleNamespace(id=host_id, products=list(products))


def test_detect_one_row_per_product_per_host():
    report = detect(
        [
            _host("h1", _product("app", True), _product("agent", False)),
            _host("h2", _product("app", None)),
        ]
    )
    assert report.rows == [
        InstrumentationRow("h1", "app", True),
        InstrumentationRow("h1", "agent", False),
        InstrumentationRow("h2", "app", None),
    ]
    assert [r.product for r in report.instrumented()] == ["app"]
    assert [(r.host_id, r.product) for r in report.missing()] == [("h1", "agent"), ("h2", "app")]


def test_instrumented_products_filters_by_true_only():
    a, b, c = _product("a", True), _product("b", False), _product("c", None)
    assert instrumented_products(_host("h", a, b, c)) == [a]


def test_describe_labels_unknown_with_the_remedy():
    text = detect([_host("h1", _product("app", None))]).describe()
    assert "h1" in text
    assert "app" in text
    assert "unknown" in text
    assert "Product.instrumented" in text


def test_table_is_a_rich_table_with_three_columns():
    from rich.table import Table

    table = detect([_host("h1", _product("app", True))]).table()
    assert isinstance(table, Table)
    assert [c.header for c in table.columns] == ["host", "product", "instrumented"]


def test_table_caption_carries_the_remedy_only_when_unknown():
    unknown_table = detect([_host("h1", _product("app", None))]).table()
    assert unknown_table.caption is not None
    assert "Product.instrumented" in unknown_table.caption

    known_table = detect([_host("h1", _product("app", True))]).table()
    assert known_table.caption is None


def _report(*verdicts):
    return InstrumentationReport(
        [InstrumentationRow("h1", f"p{i}", v) for i, v in enumerate(verdicts)]
    )


def test_auto_some_with_config_turns_on_and_logs_info(caplog):
    with caplog.at_level(logging.INFO, logger="otto.coverage.instrumentation"):
        assert (
            decide_coverage(None, _report(True, True), has_cov_config=True, command="otto test")
            is True
        )
    assert any("instrumented" in r.message for r in caplog.records if r.levelno == logging.INFO)


def test_auto_some_without_config_warns_and_stays_off(caplog):
    with caplog.at_level(logging.WARNING, logger="otto.coverage.instrumentation"):
        assert (
            decide_coverage(None, _report(True), has_cov_config=False, command="otto test") is False
        )
    warning = next(r for r in caplog.records if r.levelno == logging.WARNING)
    assert "[coverage]" in warning.message
    assert "p0" in warning.message


def test_auto_none_is_off_and_quiet(caplog):
    with caplog.at_level(logging.DEBUG, logger="otto.coverage.instrumentation"):
        assert (
            decide_coverage(None, _report(False, None), has_cov_config=True, command="otto test")
            is False
        )
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]
    assert any(r.levelno == logging.DEBUG for r in caplog.records)


def test_forced_on_with_none_instrumented_raises_listing_every_product():
    with pytest.raises(CoverageNotInstrumentedError) as excinfo:
        decide_coverage(True, _report(False, None), has_cov_config=True, command="otto test --cov")
    assert isinstance(excinfo.value, CoverageConfigError)
    msg = str(excinfo.value)
    assert "otto test --cov" in msg
    assert "p0" in msg
    assert "p1" in msg
    assert "unknown" in msg


def test_forced_on_with_no_products_at_all_raises():
    with pytest.raises(CoverageNotInstrumentedError, match="no products"):
        decide_coverage(True, _report(), has_cov_config=True, command="otto cov get")


def test_partial_warns_listing_the_missing_and_proceeds(caplog):
    with caplog.at_level(logging.WARNING, logger="otto.coverage.instrumentation"):
        assert (
            decide_coverage(
                True, _report(True, False, None), has_cov_config=True, command="otto test --cov"
            )
            is True
        )
    warning = next(r for r in caplog.records if r.levelno == logging.WARNING)
    assert "p1" in warning.message
    assert "p2" in warning.message
    assert "p0" not in warning.message
    assert "Product.instrumented" in warning.message


def test_auto_partial_warns_listing_missing_and_turns_on(caplog):
    with caplog.at_level(logging.WARNING, logger="otto.coverage.instrumentation"):
        assert (
            decide_coverage(None, _report(True, False), has_cov_config=True, command="otto test")
            is True
        )
    warning = next(r for r in caplog.records if r.levelno == logging.WARNING)
    assert "p1" in warning.message
    assert "p0" not in warning.message


def test_forced_off_never_looks():
    assert (
        decide_coverage(False, _report(False, None), has_cov_config=True, command="otto test")
        is False
    )


def test_detect_for_lab_with_coverage_config_passes_pattern_and_containers(monkeypatch):
    import re

    pattern = re.compile("sensor.*")
    calls = {}
    host = _host("h1", _product("app", True))

    def fake_get_repos():
        return ["repo"]

    def fake_get_cov_config(repos):
        assert repos == ["repo"]
        return {"hosts": "sensor.*"}

    def fake_load_hosts_pattern(cov_config):
        assert cov_config == {"hosts": "sensor.*"}
        return pattern

    def fake_all_hosts(*, pattern, include_containers):
        calls["pattern"] = pattern
        calls["include_containers"] = include_containers
        return [host]

    monkeypatch.setattr("otto.config.get_repos", fake_get_repos)
    monkeypatch.setattr("otto.config.coverage_settings.get_cov_config", fake_get_cov_config)
    monkeypatch.setattr("otto.config.coverage_settings.load_hosts_pattern", fake_load_hosts_pattern)
    monkeypatch.setattr("otto.config.all_hosts", fake_all_hosts)

    report = detect_for_lab()

    assert calls == {"pattern": pattern, "include_containers": True}
    assert report.rows == [InstrumentationRow("h1", "app", True)]


def test_detect_for_lab_without_coverage_config_defaults_to_none_pattern(monkeypatch):
    calls = {}
    host = _host("h2", _product("agent", False))

    def fake_get_repos():
        return []

    def fake_get_cov_config(repos):
        assert repos == []
        return {}

    def fake_load_hosts_pattern(cov_config):
        assert cov_config == {}

    def fake_all_hosts(*, pattern, include_containers):
        calls["pattern"] = pattern
        calls["include_containers"] = include_containers
        return [host]

    monkeypatch.setattr("otto.config.get_repos", fake_get_repos)
    monkeypatch.setattr("otto.config.coverage_settings.get_cov_config", fake_get_cov_config)
    monkeypatch.setattr("otto.config.coverage_settings.load_hosts_pattern", fake_load_hosts_pattern)
    monkeypatch.setattr("otto.config.all_hosts", fake_all_hosts)

    report = detect_for_lab()

    assert calls == {"pattern": None, "include_containers": True}
    assert report.rows == [InstrumentationRow("h2", "agent", False)]


def test_refusal_carries_the_report_as_structure_not_only_as_text():
    """The console renders the TABLE, so the verdicts must survive as objects.

    `str(e)` keeps the plain listing for the run log and for a library caller;
    `e.report` is what `otto.cli.invoke.render_instrumentation_refusal` needs.
    """
    report = _report(False, None)
    with pytest.raises(CoverageNotInstrumentedError) as excinfo:
        decide_coverage(True, report, has_cov_config=True, command="otto test --cov")
    assert excinfo.value.report is report


def test_refusal_with_no_products_carries_the_empty_report_too():
    with pytest.raises(CoverageNotInstrumentedError) as excinfo:
        decide_coverage(True, _report(), has_cov_config=True, command="otto cov get")
    assert excinfo.value.report is not None
    assert excinfo.value.report.rows == []


def test_the_error_still_constructs_and_pickles_without_a_report():
    """`report` is optional: every existing raise site and a pickle round trip are unchanged."""
    import pickle

    bare = CoverageNotInstrumentedError("nothing instrumented")
    assert bare.report is None
    assert str(bare) == "nothing instrumented"
    revived = pickle.loads(pickle.dumps(bare))  # noqa: S301 — round trip of an error this test just built, not untrusted input
    assert str(revived) == "nothing instrumented"
    assert revived.report is None


def test_table_escapes_rich_markup_in_the_caption_and_the_cells():
    """The caption names `[[products]]`; rich deletes `[word]` unless it is escaped.

    Asserted on the table's own strings rather than on a render, so this fails
    at the source of the bug; `tests/unit/cli/test_error_render.py` pins what
    the reader actually sees.
    """
    from rich.markup import escape

    caption = detect([_host("h1", _product("app", None))]).table().caption
    assert caption == escape(caption.replace("\\", ""))
    assert "[[products]]" in caption.replace("\\", "")

    bracketed = detect([_host("h[1]", _product("a[pp]", True))]).table()
    assert [cell for col in bracketed.columns for cell in col._cells][:2] == [
        escape("h[1]"),
        escape("a[pp]"),
    ]
