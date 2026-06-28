"""Tests for the --as-user shell-completion callback."""


def test_username_completer_prefers_cache(monkeypatch):
    import otto.configmodule as cm

    monkeypatch.setattr(
        cm,
        "get_completion_names",
        lambda: {"usernames": ["alice", "alfred", "bob"]},
    )
    from otto.cli.main import _username_completer

    assert _username_completer(None, "al") == ["alfred", "alice"]


def test_username_completer_falls_back_to_live(monkeypatch):
    import otto.configmodule as cm
    import otto.configmodule.completion_cache as cc

    monkeypatch.setattr(cm, "get_completion_names", lambda: None)
    monkeypatch.setattr(cm, "get_repos", list)
    monkeypatch.setattr(cc, "collect_reservation_usernames", lambda repos: ["zoe", "zed"])
    from otto.cli.main import _username_completer

    assert _username_completer(None, "z") == ["zed", "zoe"]
