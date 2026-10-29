"""The ExpectCollector (the conformance engine) and the ``expect`` fixture built on it."""

import logging
import os

import pytest

from otto.suite.expect import ExpectCollector, expect
from tests.unit.suite._inner import run_inner

pytest_plugins = ["pytester"]


class TestExpectCollector:
    def test_passing_expect_records_nothing(self):
        c = ExpectCollector()
        c.expect(True)
        c.expect(True, "should not record")
        assert c.failures == []

    def test_failing_expect_records_report(self):
        c = ExpectCollector()
        x = 42
        c.expect(x == 99, "math is broken")
        assert len(c.failures) == 1
        report = c.failures[0]
        assert "math is broken" in report
        assert "Values: x = 42" in report  # the one name the call reads

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

    def test_a_failure_line_keeps_its_colon_words_literal(self, caplog):
        """The source line is foreign text: ``:x:`` must not become an emoji."""
        from otto.logger.formatters import RichFormatter

        logger = logging.getLogger("otto.test.expect")
        c = ExpectCollector(logger=logger)
        passwd = "daemon"
        with caplog.at_level(logging.WARNING, logger="otto.test.expect"):
            c.expect(passwd.startswith("root:x:0:0"), "see :warning: [red]")
        (record,) = [r for r in caplog.records if "EXPECT FAILED" in r.message]
        rendered = RichFormatter(fmt="{message}").format(record)
        assert "root:x:0:0" in rendered
        assert ":warning: [red]" in rendered

    def test_module_level_expect_uses_explicit_collector(self):
        c = ExpectCollector()
        expect(2 + 2 == 5, "via module fn", collector=c)
        assert len(c.failures) == 1
        assert "via module fn" in c.failures[0]


# ── what a failing report shows: only the names the call reads ─────────────

PLANTED = "PLANTED-SECRET-7f3a"


def _result(**overrides):
    from otto.result import CommandResult
    from otto.utils import Status

    fields = {"status": Status.Failed, "value": "x", "command": "ping", "retcode": 1}
    return CommandResult(**{**fields, **overrides})


@pytest.fixture
def ctx_with_planted_password():
    """An ``OttoContext`` whose lab holds a host whose cred holds the planted password."""
    from otto.host.element import Element
    from otto.host.login_proxy import Cred
    from otto.host.unix_host import UnixHost
    from otto.labs.loader import Lab
    from otto.logger.mode import LogMode
    from otto.run_context import OttoContext

    lab = Lab(name="bench")
    lab.add_host(
        UnixHost(
            ip="10.10.200.11",
            element=Element("router1"),
            creds=[Cred(login="admin", password=PLANTED)],
            log=LogMode.QUIET,
        )
    )
    return OttoContext(lab=lab)


def _run_file(tmp_path, source: str, **names) -> str:
    """Run *source* from a real file with *names* and a collector ``c``; return its one report."""
    path = tmp_path / "case.py"
    path.write_text(source, encoding="utf-8")
    c = ExpectCollector()
    exec(compile(source, str(path), "exec"), {"c": c, **names})  # noqa: S102 — runs a test-written file
    (report,) = c.failures
    return report


def _values(report: str) -> str:
    """The text after ``Values: `` on the report's Values line."""
    (line,) = [ln for ln in report.splitlines() if ln.strip().startswith("Values: ")]
    return line.strip().removeprefix("Values: ")


class TestValuesShowOnlyWhatTheCallReads:
    def test_values_shows_only_names_the_call_reads(self):
        c = ExpectCollector()
        res = _result(retcode=1, value="x")
        other = 5
        c.expect(res.retcode == 0, "ping failed")
        r = c.failures[0]
        assert "Values: res = <CommandResult" in r, r
        assert "other" not in r, r
        assert "Locals:" not in r, r
        assert other == 5

    def test_ctx_is_summarised_and_password_never_printed(self, ctx_with_planted_password):
        ctx = ctx_with_planted_password
        c = ExpectCollector()
        c.expect(ctx is None)
        assert "ctx = <OttoContext lab=bench>" in c.failures[0], c.failures[0]
        assert PLANTED not in c.failures[0]

    def test_credential_named_value_is_masked(self):
        password = PLANTED
        api_token = PLANTED
        c = ExpectCollector()
        c.expect(password == "x" and api_token == "y")
        assert "password = '***'" in c.failures[0], c.failures[0]
        assert "api_token = '***'" in c.failures[0], c.failures[0]
        assert PLANTED not in c.failures[0]

    @pytest.mark.parametrize(
        "name",
        [
            "password",
            "passwd",
            "db_password",
            "passphrase",
            "api_token",
            "secret_key",
            "pw",
            "pwd",
            "db_pwd",
            "ssh_pw",
            "PWD",
            "tokens_left",
            "PASSWORD",
        ],
    )
    def test_a_credential_looking_name_is_masked(self, name, tmp_path):
        """``tokens_left`` is masked on purpose: any name holding ``token`` is."""
        r = _run_file(tmp_path, f"c.expect({name} == 'x')\n", **{name: PLANTED})
        assert f"{name} = '***'" in r, r
        assert PLANTED not in r, r

    @pytest.mark.parametrize(
        "name", ["passed", "passes", "bypass", "compass", "keyword", "pwdx", "upward"]
    )
    def test_a_lookalike_name_is_shown(self, name, tmp_path):
        r = _run_file(tmp_path, f"c.expect({name} == 'x')\n", **{name: "shown-value"})
        assert f"{name} = 'shown-value'" in r, r

    def test_a_credential_key_in_a_mapping_is_masked_at_any_depth(self):
        opts = {"password": PLANTED, "user": "admin"}
        layered = {"outer": {"api_token": PLANTED}}  # as deep as the repr reaches
        deeper = [{"outer": {"api_token": PLANTED}}]  # past it: elided, never shown
        c = ExpectCollector()
        c.expect(opts is None and layered is None and deeper is None)
        r = c.failures[0]
        assert "'password': '***'" in r, r
        assert "'user': 'admin'" in r, r
        assert "'api_token': '***'" in r, r
        assert PLANTED not in r, r

    def test_a_mapping_subclass_is_masked_too(self):
        from collections import OrderedDict

        opts = OrderedDict(secret=PLANTED)
        c = ExpectCollector()
        c.expect(opts is None)
        assert PLANTED not in c.failures[0], c.failures[0]
        assert "'secret': '***'" in c.failures[0], c.failures[0]

    def test_a_raising_class_property_still_records_the_failure(self):
        """An unbound lazy proxy: reading ``__class__`` raises. The check must still record."""

        class Proxy:
            @property
            def __class__(self):
                raise RuntimeError("unbound proxy")

        proxy = Proxy()
        c = ExpectCollector()
        c.expect(proxy is None, "proxy check")
        (r,) = c.failures
        assert "Message: proxy check" in r, r
        assert "c.expect(proxy is None" in r, r
        assert "Values:" not in r, r

    def test_a_parser_that_blows_up_still_records_the_failure(self, monkeypatch):
        import otto.suite.expect as expect_mod

        def boom(*_args, **_kwargs):
            raise RecursionError("too deep")

        expect_mod._parse_file.cache_clear()
        monkeypatch.setattr(expect_mod.ast, "parse", boom)
        value = 3
        c = ExpectCollector()
        c.expect(value == 4, "parser down")
        (r,) = c.failures
        assert "Message: parser down" in r, r
        assert "c.expect(value == 4" in r, r
        assert "Values:" not in r, r

    def test_the_parsed_file_is_memoised_and_an_edit_reparses(self, tmp_path, monkeypatch):
        import otto.suite.expect as expect_mod

        calls = []
        real_parse = expect_mod.ast.parse

        def counting(*args, **kwargs):
            calls.append(1)
            return real_parse(*args, **kwargs)

        monkeypatch.setattr(expect_mod.ast, "parse", counting)
        path = tmp_path / "memo_case.py"
        path.write_text("c.expect(v == 1)\n")
        c = ExpectCollector()
        code = compile(path.read_text(), str(path), "exec")
        exec(code, {"c": c, "v": 2})  # noqa: S102 — runs a test-written file
        exec(code, {"c": c, "v": 2})  # noqa: S102 — runs a test-written file
        assert len(calls) == 1
        path.write_text("c.expect(w == 1)  # edited\n")
        os.utime(path, (1, 1))
        exec(compile(path.read_text(), str(path), "exec"), {"c": c, "w": 5})  # noqa: S102
        assert len(calls) == 2
        assert "Values: w = 5" in c.failures[-1], c.failures[-1]

    def test_non_ascii_before_a_multiline_call_keeps_its_indent(self, tmp_path):
        """``col_offset`` counts UTF-8 bytes; the report's indent counts characters."""
        source = 's = "é"; c.expect(\n    v == 1,\n)\n'
        r = _run_file(tmp_path, source, v=2, s="é")
        assert "\n  " + " " * len('s = "é"; ') + "c.expect(\n" in r, r

    def test_cred_local_never_prints_password(self):
        from otto.host.login_proxy import Cred

        cred = Cred(login="admin", password=PLANTED)
        c = ExpectCollector()
        c.expect(cred is None)
        assert "cred = " in c.failures[0], c.failures[0]
        assert PLANTED not in c.failures[0]

    def test_attribute_chain_is_not_evaluated(self):
        class Boom:
            @property
            def p(self):
                raise AssertionError("property ran")

        b = Boom()
        c = ExpectCollector()
        c.expect(b.p if False else False)
        assert "b = " in c.failures[0], c.failures[0]

    def test_multiline_call_is_read_whole(self):
        alpha, beta, gamma = 1, 2, "g"
        c = ExpectCollector()
        c.expect(
            alpha == beta,
            f"{gamma}",
        )
        r = c.failures[0]
        assert _values(r) == "alpha = 1, beta = 2, gamma = 'g'", r
        # the call's lines, all of them, dedented and indented two spaces
        assert '\n  c.expect(\n      alpha == beta,\n      f"{gamma}",\n  )\n' in r, r

    def test_values_cap_100_chars(self):
        big = "a" * 10_000
        nested = [[i, [i, [i]]] for i in range(1_000)]
        c = ExpectCollector()
        c.expect(big == "")
        c.expect(nested == [])
        for report, name in zip(c.failures, ["big", "nested"], strict=True):
            rendered = _values(report).removeprefix(f"{name} = ")
            assert len(rendered) <= 101, rendered
            assert rendered.endswith("…") or len(rendered) <= 100, rendered
        big_rendered = _values(c.failures[0]).removeprefix("big = ")
        assert big_rendered == ("'" + "a" * 99)[:100] + "…", big_rendered
        assert len(big_rendered) == 101

    def test_ambiguous_call_omits_values(self):
        a, b = 1, 2
        c = ExpectCollector()
        [c.expect(a == 0), c.expect(b == 0)]  # two calls on one line: the case under test
        assert len(c.failures) == 2
        for r in c.failures:
            assert "Values:" not in r, r
            assert "Locals:" not in r, r

    def test_helper_call_reads_helper_frame(self):
        """A wrapper's ``_stack_offset=2`` reports the wrapper's CALLER and the names it reads."""

        def check(collector, value):
            collector.expect(value == 1, "helper says no", _stack_offset=2)

        c = ExpectCollector()
        reading = 3
        unrelated = 9
        check(c, reading)
        r = c.failures[0]
        assert "check(c, reading)" in r, r
        assert "reading = 3" in r, r
        assert "value =" not in r, r
        assert "unrelated" not in r, r
        assert unrelated == 9

    def test_unreadable_source_omits_values(self):
        c = ExpectCollector()
        code = compile("c.expect(v == 1)", "<generated>", "exec")
        exec(code, {"c": c, "v": 2})  # noqa: S102 — code with no readable source is the case under test
        r = c.failures[0]
        assert "Values:" not in r, r
        assert "Locals:" not in r, r

    def test_a_raising_repr_or_summary_never_breaks_the_report(self):
        class BadRepr:
            def __repr__(self):
                raise RuntimeError("no repr")

        class BadSummary:
            def __otto_summary__(self):
                raise RuntimeError("no summary")

        bad_repr, bad_summary = BadRepr(), BadSummary()
        c = ExpectCollector()
        c.expect(bad_repr is None and bad_summary is None)
        r = c.failures[0]
        assert "bad_repr = <" in r, r
        assert "bad_summary = <BadSummary>" in r, r

    def test_the_logged_warning_shows_the_whole_call(self, caplog):
        logger = logging.getLogger("otto.test.expect")
        c = ExpectCollector(logger=logger)
        first, second = 1, 2
        with caplog.at_level(logging.WARNING, logger="otto.test.expect"):
            c.expect(
                first == second,
                "spread out",
            )
        (record,) = [r for r in caplog.records if "EXPECT FAILED" in r.message]
        assert "first == second," in record.message, record.message


class TestOttoSummaries:
    """The one-line identity each domain object gives a failing expect report."""

    def test_command_result_shows_status_retcode_and_a_visible_value_prefix(self):
        res = _result(value="line1\r\n\x1b[0m" + "z" * 200, retcode=2)
        summary = res.__otto_summary__()
        assert summary.startswith(
            "<CommandResult status=Failed retcode=2 value='line1\\r\\n\\x1b[0m"
        ), summary
        assert summary.endswith("…>"), summary
        assert len(summary) < 110, summary

    def test_a_short_value_is_not_cut(self):
        assert _result(value="ok").__otto_summary__() == (
            "<CommandResult status=Failed retcode=1 value='ok'>"
        )

    def test_not_run_result_summary_does_not_read_value(self):
        from otto.result import NotRunResult
        from otto.utils import Status

        res = NotRunResult(status=Status.NotRun, command="ls", host_name="h")
        assert res.__otto_summary__() == "<NotRunResult status=NotRun retcode=-1 value=<not run>>"

    def test_host_summary_is_its_class_and_display_name(self, ctx_with_planted_password):
        (host,) = ctx_with_planted_password.lab.hosts.values()
        assert host.__otto_summary__() == f"<UnixHost {host}>"
        assert PLANTED not in host.__otto_summary__()

    def test_lab_summary_is_its_name(self, ctx_with_planted_password):
        assert ctx_with_planted_password.lab.__otto_summary__() == "<Lab bench>"


# ── the callable form: ``expect(cond, msg)`` (spec §5.4) ─────────────────────


def test_call_records_like_expect_and_points_at_the_caller() -> None:
    collector = ExpectCollector()
    answer = 41
    collector(answer == 42, "off by one")
    assert len(collector.failures) == 1
    report = collector.failures[0]
    assert "Message: off by one" in report
    assert "Values: answer = 41" in report, report  # the CALLER's names, not __call__'s
    assert 'collector(answer == 42, "off by one")' in report


def test_call_with_a_truthy_condition_records_nothing() -> None:
    collector = ExpectCollector()
    collector(True)
    assert collector.failures == []


# ── the ``expect`` fixture, through a real inner session (spec §5.4) ────────


class _PhaseRecorder:
    """Inner-session plugin: remembers (when, outcome) per test report."""

    def __init__(self) -> None:
        self.reports: list[tuple[str, str]] = []

    def pytest_runtest_logreport(self, report) -> None:
        self.reports.append((report.when, report.outcome))


def _run_recording(pytester, otto_plugins, **files):
    """``run_inner`` with a phase recorder registered alongside otto's plugins."""
    recorder = _PhaseRecorder()
    result = run_inner(pytester, [*otto_plugins, recorder], **files)
    return result, recorder.reports


def test_a_passing_expect_does_not_fail(pytester, otto_plugins) -> None:
    body = "async def test_ok(expect):\n    expect(True)\n    expect(1 == 1)\n    expect('x')\n"
    run_inner(pytester, otto_plugins, test_pass=body).assert_outcomes(passed=1)


def test_a_failing_expect_lets_the_body_continue(pytester, otto_plugins) -> None:
    body = """\
import pathlib

async def test_continues(expect):
    expect(False)
    pathlib.Path(__file__).with_name("reached").write_text("yes")
"""
    run_inner(pytester, otto_plugins, test_continue=body).assert_outcomes(failed=1)
    assert (pytester.path / "reached").read_text() == "yes"


def test_failures_fail_the_call_phase_not_teardown(pytester, otto_plugins) -> None:
    """``1 failed``, in the CALL phase. Red if the summary is raised from a
    fixture's teardown (a ``passed`` call plus a ``failed`` teardown)."""
    body = "async def test_soft(expect):\n    expect(False, 'soft')\n"
    result, reports = _run_recording(pytester, otto_plugins, test_phase=body)
    result.assert_outcomes(failed=1)
    assert ("call", "failed") in reports, reports
    assert ("teardown", "failed") not in reports, reports
    assert ("setup", "passed") in reports, reports


def test_a_hard_assert_in_the_body_wins(pytester, otto_plugins) -> None:
    """The body's own AssertionError is the failure; the soft one was already logged."""
    body = """\
async def test_hard(expect):
    expect(False, "soft failure recorded")
    raise AssertionError("hard failure wins")
"""
    result = run_inner(pytester, otto_plugins, test_hard=body)
    result.assert_outcomes(failed=1)
    out = result.stdout.str()
    assert "hard failure wins" in out
    assert "1 expectation(s) failed" not in out


def test_every_failure_is_recorded_with_its_source_and_message(pytester, otto_plugins) -> None:
    """Three soft failures among passes, each report naming its file, line, values and msg."""
    body = """\
import json
import pathlib

async def test_mixed(expect):
    val = 42
    expect(True)
    expect(val == 99, "hostname missing from config")
    expect(True)
    expect(False, "two")
    expect(False, "three")
    pathlib.Path(__file__).with_name("failures.json").write_text(json.dumps(expect.failures))
"""
    run_inner(pytester, otto_plugins, test_mix=body).assert_outcomes(failed=1)
    import json

    failures = json.loads((pytester.path / "failures.json").read_text())
    assert len(failures) == 3
    first = failures[0]
    assert "test_mix.py" in first
    assert "expect(val == 99" in first
    assert "Values: val = 42" in first
    assert "hostname missing from config" in first


def test_failures_reset_between_tests(pytester, otto_plugins) -> None:
    body = """\
import pathlib

async def test_first(expect):
    expect(False, "only mine")

async def test_second(expect):
    pathlib.Path(__file__).with_name("count").write_text(str(len(expect.failures)))
"""
    run_inner(pytester, otto_plugins, test_reset=body).assert_outcomes(passed=1, failed=1)
    assert (pytester.path / "count").read_text() == "0"


def test_a_method_of_a_plain_class_may_use_expect(pytester, otto_plugins) -> None:
    body = """\
class TestPlain:
    async def test_soft(self, expect):
        expect(False, "class soft failure")
"""
    result, reports = _run_recording(pytester, otto_plugins, test_plain_class=body)
    result.assert_outcomes(failed=1)
    assert ("call", "failed") in reports


def test_expect_composes_with_retry(pytester, otto_plugins) -> None:
    """The collector resets per attempt, so a retried body whose second attempt is
    clean PASSES. Red if the check lives in ``pytest_runtest_call`` (the retry
    never re-enters it) or the collector is not reset."""
    body = """\
import pathlib

import pytest

COUNTER = pathlib.Path(__file__).with_name("attempts")

@pytest.mark.retry(2)
async def test_flaky_soft(expect):
    attempts = int(COUNTER.read_text()) if COUNTER.exists() else 0
    COUNTER.write_text(str(attempts + 1))
    expect(attempts >= 1, "first attempt is soft-red")
"""
    result, reports = _run_recording(pytester, otto_plugins, test_retry_expect=body)
    assert (pytester.path / "attempts").read_text() == "2"
    result.assert_outcomes(passed=1)
    assert ("call", "passed") in reports
