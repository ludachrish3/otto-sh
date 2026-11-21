"""The renderer slot: one evaluation per request, its limits, its errors, and its memo.

Every render here runs through the binding this platform installed; CI runs
the directory under both (``noxfile.py::templating_go``), so each assertion on
an error's text holds for the C++ and the Go binding alike.
"""

import contextvars
import json
import sys
import threading
from pathlib import Path

import pytest

from otto.inventory.config import ProtectedPaths
from otto.templating import render as render_module
from otto.templating.errors import RenderCancelled, RenderError
from otto.templating.memo import (
    RenderKey,
    RenderMemo,
    RenderOutcome,
    cancellation,
    current_cancellation,
)
from otto.templating.render import RenderLimits, RenderResult, render
from tests._fixtures.paths import TESTS_ROOT

from ._render_helpers import NOTHING_PROTECTED, protecting, req, root_with

LARGE_FLEET = TESTS_ROOT / "_fixtures" / "templating" / "large_fleet.jsonnet"


def test_trace_is_left_alone_and_unrelated_stderr_is_never_captured(
    capfd: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    render(req(tmp_path, "function(otto) std.trace('T-1', {a: 1})", tla={"otto": {}}))
    sys.stderr.write("UNRELATED\n")
    err = capfd.readouterr().err
    assert "T-1" in err  # visible, documented
    assert "UNRELATED" in err  # nothing redirected


def test_tla_none_passes_no_top_level_argument(tmp_path: Path) -> None:
    out = render(req(tmp_path, "function(otto={project: 'none'}) otto.project"))
    assert json.loads(out.text) == "none"
    passed = render(req(tmp_path, "function(otto) otto.project", tla={"otto": {"project": "p"}}))
    assert json.loads(passed.text) == "p"


def test_the_result_is_the_bindings_unparsed_text_and_names_what_it_read(tmp_path: Path) -> None:
    lib = root_with(tmp_path, {"a.libsonnet": b"{x: 1}"})
    request = req(tmp_path, "(import 'a.libsonnet') + (import 'a.libsonnet')", roots=(lib,))
    out = render(request)
    assert isinstance(out, RenderResult)
    assert json.loads(out.text) == {"x": 1}
    assert out.read == (request.entry.resolve(), (lib / "a.libsonnet").resolve())


def test_error_names_file_and_line(tmp_path: Path) -> None:
    request = req(tmp_path, "{\n  a: error 'boom',\n}")
    with pytest.raises(RenderError) as caught:
        render(request)
    assert caught.value.origin == request.entry
    assert caught.value.line == 2
    assert str(caught.value) == f"{request.entry}:2: boom"


def test_a_syntax_error_names_file_and_line(tmp_path: Path) -> None:
    request = req(tmp_path, "{a: 1,\n b: }")
    with pytest.raises(RenderError) as caught:
        render(request)
    assert (caught.value.origin, caught.value.line) == (request.entry, 2)
    assert str(caught.value).startswith(f"{request.entry}:2: ")
    assert "\n" not in str(caught.value)


def test_an_error_in_an_import_names_the_imported_file(tmp_path: Path) -> None:
    lib = root_with(tmp_path, {"bad.libsonnet": b"{\n\n  x: error 'deep',\n}\n"})
    with pytest.raises(RenderError) as caught:
        render(req(tmp_path, "(import 'bad.libsonnet').x", roots=(lib,)))
    assert caught.value.origin == (lib / "bad.libsonnet").resolve()
    assert caught.value.line == 3
    assert str(caught.value) == f"{(lib / 'bad.libsonnet').resolve()}:3: deep"


def test_an_unreadable_entry_is_a_render_error(tmp_path: Path) -> None:
    request = req(tmp_path, "{}")
    request.entry.unlink()
    with pytest.raises(RenderError, match=r"e\.jsonnet") as caught:
        render(request)
    assert (caught.value.origin, caught.value.line) == (request.entry, None)


def test_no_ext_vars_or_natives(tmp_path: Path) -> None:
    with pytest.raises(RenderError, match="external variable: x"):
        render(req(tmp_path, "std.extVar('x')"))
    # Jsonnet answers an unregistered native with null; none is registered.
    assert json.loads(render(req(tmp_path, "std.native('x')")).text) is None
    with pytest.raises(RenderError):
        render(req(tmp_path, "std.native('x')(1)"))


def test_real_limits(tmp_path: Path) -> None:
    with pytest.raises(RenderError, match="max stack"):
        render(req(tmp_path, "local f(n) = f(n+1); f(0)"))
    with pytest.raises(RenderError, match=r"over the 64-byte output limit"):
        render(req(tmp_path, "std.repeat('x', 1000)", limits=RenderLimits(max_output_bytes=64)))


def test_the_large_fleet_renders_inside_the_default_limits(tmp_path: Path) -> None:
    """The measured fleet the defaults are set from (RenderLimits' docstring) renders."""
    out = render(req(tmp_path, LARGE_FLEET.read_text()))
    document = json.loads(out.text)
    assert len(document["labs"]) == 500
    assert len(document["elements"]) == 2000
    assert len(out.text.encode()) * 8 <= RenderLimits().max_output_bytes


def test_cancelled_token_never_enters_the_evaluator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    real = render_module._evaluate

    def counting(*args: object, **kwargs: object) -> str:
        calls.append("evaluate")
        return real(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(render_module, "_evaluate", counting)
    request = req(tmp_path, "{}")
    with cancellation(lambda: True), pytest.raises(RenderCancelled):
        render(request)
    assert calls == []
    render(request)
    assert calls == ["evaluate"]


# -- the memo ---------------------------------------------------------------


def _key(scope: str = "/proj-a", *, protected: ProtectedPaths = NOTHING_PROTECTED) -> RenderKey:
    return RenderKey(
        scope=Path(scope),
        entry=Path(scope) / "lab_data" / "fleet.jsonnet",
        import_roots=(Path(scope) / "lab_data" / "lib",),
        inventories=("default",),
        project=Path(scope).name,
        protected=protected,
        library_digest="0" * 64,
    )


def _result(text: str = "{}") -> RenderResult:
    return RenderResult(text=text, read=())


def test_the_memo_renders_each_key_once() -> None:
    memo = RenderMemo()
    runs: list[str] = []

    def run() -> RenderResult:
        runs.append("run")
        return _result()

    first = memo.get_or_render(_key(), run)
    again = memo.get_or_render(_key(), run)
    assert first is again
    assert first == RenderOutcome(result=_result())
    assert runs == ["run"]


def test_a_render_error_settles_as_the_keys_error() -> None:
    memo = RenderMemo()

    def run() -> RenderResult:
        raise RenderError("e.jsonnet:2: boom", origin=Path("e.jsonnet"), line=2)

    outcome = memo.get_or_render(_key(), run)
    assert outcome == RenderOutcome(error="e.jsonnet:2: boom")


def test_memo_settle_never_replaces_a_timeout() -> None:
    memo = RenderMemo()

    def run() -> RenderResult:
        memo.cancel(scope=Path("/proj-a"), reason="deadline passed")  # the deadline, mid-render
        return _result()

    outcome = memo.get_or_render(_key(), run)
    assert outcome == RenderOutcome(error="deadline passed", timed_out=True)
    assert memo.get_or_render(_key(), _result) is outcome


def test_cancelled_scope_starts_no_render() -> None:
    memo = RenderMemo()
    runs: list[str] = []
    memo.cancel(scope=Path("/proj-a"), reason="deadline passed")

    def run() -> RenderResult:
        runs.append("run")
        return _result()

    outcome = memo.get_or_render(_key(), run)
    assert outcome == RenderOutcome(error="deadline passed", timed_out=True)
    assert runs == []
    assert memo.cancelled(Path("/proj-a"))()


def test_cancel_is_per_project_scope() -> None:
    memo = RenderMemo()
    memo.cancel(scope=Path("/proj-a"), reason="deadline passed")
    outcome = memo.get_or_render(_key("/proj-b"), _result)
    assert outcome == RenderOutcome(result=_result())
    assert not memo.cancelled(Path("/proj-b"))()
    assert memo.publish(scope=Path("/proj-b"), action=lambda: None)


def test_render_cancelled_settles_as_timed_out() -> None:
    memo = RenderMemo()

    def run() -> RenderResult:
        raise RenderCancelled("cancelled before the evaluator ran")

    outcome = memo.get_or_render(_key(), run)
    assert outcome == RenderOutcome(error="cancelled before the evaluator ran", timed_out=True)


def test_an_unexpected_failure_propagates_and_leaves_the_key_unsettled() -> None:
    memo = RenderMemo()

    def broken() -> RenderResult:
        raise KeyError("a bug, not a render failure")

    with pytest.raises(KeyError):
        memo.get_or_render(_key(), broken)
    assert memo.get_or_render(_key(), _result).result == _result()


def test_render_key_holds_the_resolved_catalog_not_the_provider(tmp_path: Path) -> None:
    (tmp_path / "creds.json").write_text("{}")
    catalog = protecting(tmp_path, {"creds.json"})
    same = ProtectedPaths(files=frozenset(catalog.files))
    key = _key(protected=catalog)
    assert key == _key(protected=same)
    assert hash(key) == hash(_key(protected=same))
    assert key.protected is catalog

    memo = RenderMemo()
    runs: list[str] = []

    def run() -> RenderResult:
        runs.append("run")
        return _result()

    memo.get_or_render(key, run)
    memo.get_or_render(_key(protected=same), run)
    assert runs == ["run"]
    memo.get_or_render(_key(protected=NOTHING_PROTECTED), run)  # another catalog, another key
    assert runs == ["run", "run"]


def test_publish_after_cancel_runs_nothing() -> None:
    memo = RenderMemo()
    actions: list[str] = []
    memo.cancel(scope=Path("/proj-a"), reason="deadline passed")
    assert memo.publish(scope=Path("/proj-a"), action=lambda: actions.append("ran")) is False
    assert actions == []


def _write(path: Path) -> None:
    path.write_text("{}")


def test_publication_after_cancel_is_refused_before_any_write(tmp_path: Path) -> None:
    memo = RenderMemo()
    accepted = tmp_path / "accepted.json"
    refused = tmp_path / "refused.json"
    assert memo.publish(scope=Path("/proj-a"), action=lambda: _write(accepted))
    memo.cancel(scope=Path("/proj-a"), reason="deadline passed")
    assert not memo.publish(scope=Path("/proj-a"), action=lambda: _write(refused))
    assert accepted.exists()
    assert not refused.exists()


def test_cancel_never_waits_for_an_accepted_publication() -> None:
    memo = RenderMemo()
    started = threading.Event()
    release = threading.Event()
    finished: list[str] = []
    published: list[bool] = []

    def action() -> None:
        started.set()
        release.wait()
        finished.append("written")

    worker = threading.Thread(
        target=lambda: published.append(memo.publish(scope=Path("/proj-a"), action=action))
    )
    worker.start()
    started.wait()
    memo.cancel(scope=Path("/proj-a"), reason="deadline passed")  # returns while the action blocks
    assert finished == []
    assert worker.is_alive()
    assert memo.cancelled(Path("/proj-a"))()
    release.set()
    worker.join()
    assert finished == ["written"]
    assert published == [True]


# -- the operation's cancellation route ------------------------------------


def test_cancellation_restores_on_exceptional_exit() -> None:
    assert not current_cancellation()
    seen: list[bool] = []

    def failing_operation() -> None:
        with cancellation(lambda: True):
            seen.append(current_cancellation())
            raise ValueError("the operation failed")

    with pytest.raises(ValueError, match="the operation failed"):
        failing_operation()
    assert seen == [True]
    assert not current_cancellation()


def test_nested_cancellation_restores_the_outer_token() -> None:
    outer = threading.Event()
    inner = threading.Event()
    with cancellation(outer.is_set):
        with cancellation(inner.is_set):
            assert not current_cancellation()
            outer.set()
            assert current_cancellation()  # the inner sees outer-or-inner
            outer.clear()
            inner.set()
            assert current_cancellation()
        assert not current_cancellation()  # the inner token is gone
        outer.set()
        assert current_cancellation()
    assert not current_cancellation()


def test_threads_see_only_their_own_cancellation() -> None:
    seen: list[bool] = []

    def worker() -> None:
        seen.append(current_cancellation())
        with cancellation(lambda: True):
            seen.append(current_cancellation())

    with cancellation(lambda: True):
        thread = threading.Thread(target=lambda: contextvars.Context().run(worker))
        thread.start()
        thread.join()
        assert current_cancellation()
    assert seen == [False, True]
    assert not current_cancellation()
