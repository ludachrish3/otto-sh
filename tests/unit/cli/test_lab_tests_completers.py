"""Tab-completion callbacks for --lab and --tests (cache-then-live)."""


def test_lab_completer_prefers_cache(monkeypatch):
    import otto.configmodule as cm

    monkeypatch.setattr(cm, "get_completion_names", lambda: {"labs": ["tech1", "tech2", "prod"]})
    from otto.cli.main import _lab_completer

    assert _lab_completer(None, "tech") == ["tech1", "tech2"]


def test_lab_completer_falls_back_to_live(monkeypatch):
    import otto.configmodule as cm
    import otto.configmodule.completion_cache as cc

    monkeypatch.setattr(cm, "get_completion_names", lambda: None)
    monkeypatch.setattr(cm, "get_repos", list)
    monkeypatch.setattr(cc, "collect_lab_names", lambda repos: ["alpha", "beta"])
    from otto.cli.main import _lab_completer

    assert _lab_completer(None, "") == ["alpha", "beta"]


def test_lab_completer_continues_after_comma(monkeypatch):
    import otto.configmodule as cm

    monkeypatch.setattr(cm, "get_completion_names", lambda: {"labs": ["tech1", "tech2"]})
    from otto.cli.main import _lab_completer

    # First lab typed; completing the second must keep the prefix and not
    # re-offer the one already chosen.
    assert _lab_completer(None, "tech1,tech") == ["tech1,tech2"]


def test_tests_completer_prefers_cache(monkeypatch):
    import otto.configmodule as cm

    monkeypatch.setattr(
        cm,
        "get_completion_names",
        lambda: {"tests": ["test_a", "test_b", "TestX::test_a"]},
    )
    from otto.cli.test import _tests_completer

    assert _tests_completer(None, "test_") == ["test_a", "test_b"]


def test_tests_completer_falls_back_to_live(monkeypatch):
    import otto.configmodule as cm
    import otto.configmodule.completion_cache as cc

    monkeypatch.setattr(cm, "get_completion_names", lambda: None)
    monkeypatch.setattr(cm, "get_repos", list)
    monkeypatch.setattr(cc, "collect_test_names", lambda repos: ["test_smoke", "test_boot"])
    from otto.cli.test import _tests_completer

    assert _tests_completer(None, "test_") == ["test_boot", "test_smoke"]
