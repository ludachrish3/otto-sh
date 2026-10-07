"""Tab-completion callbacks: ``--lab`` (cache, then live), ``otto test``'s NAMES and ``-m``."""


def test_lab_completer_prefers_cache(monkeypatch):

    monkeypatch.setattr(
        "otto.bootstrap.get_completion_names",
        lambda: {"labs": ["tech1", "tech2", "prod"]},
    )
    from otto.cli.main import _lab_completer

    assert _lab_completer(None, "tech") == ["tech1", "tech2"]


def test_lab_completer_falls_back_to_live(monkeypatch):
    import otto.config.completion_cache as cc

    monkeypatch.setattr("otto.bootstrap.get_completion_names", lambda: None)
    monkeypatch.setattr("otto.bootstrap.get_repos", list)
    monkeypatch.setattr(cc, "collect_lab_names", lambda repos: ["alpha", "beta"])
    from otto.cli.main import _lab_completer

    assert _lab_completer(None, "") == ["alpha", "beta"]


def _serve(monkeypatch, *, names=(), markers=()):
    """Answer every ``completion_view`` with *names* and *markers*, sorted as it promises.

    What the view holds, and when it collects, is
    ``tests/unit/cli/test_test_name_completion.py``'s subject; here only the
    completers' own shaping is.
    """
    import otto.config.collected_tests as ct

    monkeypatch.setattr("otto.bootstrap.get_repos", list)
    monkeypatch.setattr(
        ct,
        "completion_view",
        lambda repos: ct.CompletionView(names=sorted(names), markers=sorted(markers)),
    )


def test_lab_completer_continues_after_plus(monkeypatch):

    monkeypatch.setattr("otto.bootstrap.get_completion_names", lambda: {"labs": ["tech1", "tech2"]})
    from otto.cli.main import _lab_completer

    # First lab typed; completing the second must keep the prefix and not
    # re-offer the one already chosen.
    assert _lab_completer(None, "tech1+tech") == ["tech1+tech2"]


def test_names_completer_completes_one_name_per_word(monkeypatch):
    """NAMES is variadic with no separator: a comma is just part of the prefix."""
    _serve(monkeypatch, names=["test_a", "test_b"])
    from otto.cli.test import _names_completer

    assert _names_completer(None, "test_a,test_") == []
    assert _names_completer(None, "test_b") == ["test_b"]


def test_names_completer_offers_the_view_by_prefix(monkeypatch):
    _serve(monkeypatch, names=["test_b", "test_a", "TestX::test_a", "TestX"])
    from otto.cli.test import _names_completer

    assert _names_completer(None, "test_") == ["test_a", "test_b"]
    assert _names_completer(None, "TestX") == ["TestX", "TestX::test_a"]


def test_markers_completer_adds_ottos_own_markers(monkeypatch):
    _serve(monkeypatch, markers=["slow", "smoke"])
    from otto.cli.test import _markers_completer
    from otto.suite.markers import OTTO_MARKERS

    assert _markers_completer(None, "") == sorted({"slow", "smoke", *OTTO_MARKERS})


def test_markers_completer_completes_inside_an_expression(monkeypatch):
    _serve(monkeypatch, markers=["slow", "smoke"])
    from otto.cli.test import _markers_completer

    assert _markers_completer(None, "smoke and s") == ["smoke and slow", "smoke and smoke"]


def test_markers_option_advertises_the_completer():
    import inspect
    from typing import get_args

    from otto.cli import test as test_module

    sig = inspect.signature(test_module._run_flags)  # the template that declares `markers`
    metadata = get_args(sig.parameters["markers"].annotation)
    option = next(m for m in metadata if hasattr(m, "autocompletion"))
    assert option.autocompletion is test_module._markers_completer


def test_names_argument_advertises_the_completer():
    import inspect
    from typing import get_args

    from otto.cli import test as test_module

    leaf = test_module.test_app.registered_commands[0].callback
    metadata = get_args(inspect.signature(leaf).parameters["names"].annotation)
    argument = next(m for m in metadata if hasattr(m, "autocompletion"))
    assert argument.autocompletion is test_module._names_completer
