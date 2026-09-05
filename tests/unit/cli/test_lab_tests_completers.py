"""Tab-completion callbacks for --lab and --tests (cache-then-live)."""


def test_lab_completer_prefers_cache(monkeypatch):
    import otto.config as cm

    monkeypatch.setattr(cm, "get_completion_names", lambda: {"labs": ["tech1", "tech2", "prod"]})
    from otto.cli.main import _lab_completer

    assert _lab_completer(None, "tech") == ["tech1", "tech2"]


def test_lab_completer_falls_back_to_live(monkeypatch):
    import otto.config as cm
    import otto.config.completion_cache as cc

    monkeypatch.setattr(cm, "get_completion_names", lambda: None)
    monkeypatch.setattr(cm, "get_repos", list)
    monkeypatch.setattr(cc, "collect_lab_names", lambda repos: ["alpha", "beta"])
    from otto.cli.main import _lab_completer

    assert _lab_completer(None, "") == ["alpha", "beta"]


def _patch_no_collected(monkeypatch):
    """Neutralize the collected-tests layer so a test exercises just the floor."""
    import otto.config as cm
    import otto.config.completion_cache as cc

    monkeypatch.setattr(cm, "get_repos", list)
    monkeypatch.setattr(cc, "read_collected_tests", lambda repos: None)
    monkeypatch.setattr(cc, "maybe_warm_collected_tests", lambda repos: None)


def _patch_tests_section(monkeypatch, names):
    """Serve *names* as the cached ``tests`` section floor.

    The `--tests` floor moved off the ``get_completion_names`` snapshot when
    the completion fast path was split: `entry()` installs the ``names``
    section alone, so this completer is the one that reads its own section —
    and this is therefore where these tests have to plant the cache.
    """
    import otto.config.cache_sections as cs

    monkeypatch.setattr(
        cs, "read_section", lambda repos, name: {"tests": names} if name == "tests" else None
    )


def test_lab_completer_continues_after_plus(monkeypatch):
    import otto.config as cm

    monkeypatch.setattr(cm, "get_completion_names", lambda: {"labs": ["tech1", "tech2"]})
    from otto.cli.main import _lab_completer

    # First lab typed; completing the second must keep the prefix and not
    # re-offer the one already chosen.
    assert _lab_completer(None, "tech1+tech") == ["tech1+tech2"]


def test_tests_completer_still_continues_after_comma(monkeypatch):
    """`--tests` keeps the comma — the separator generalization must not leak."""

    _patch_tests_section(monkeypatch, ["test_a", "test_b"])
    _patch_no_collected(monkeypatch)
    from otto.cli.test import _tests_completer

    assert _tests_completer(None, "test_a,test_") == ["test_a,test_b"]


def test_tests_completer_prefers_cache(monkeypatch):

    _patch_tests_section(monkeypatch, ["test_a", "test_b", "TestX::test_a"])
    _patch_no_collected(monkeypatch)
    from otto.cli.test import _tests_completer

    assert _tests_completer(None, "test_") == ["test_a", "test_b"]


def test_tests_completer_falls_back_to_live(monkeypatch):
    import otto.config.cache_sections as cs
    import otto.config.completion_cache as cc

    monkeypatch.setattr(cs, "read_section", lambda repos, name: None)
    monkeypatch.setattr(cc, "collect_test_names", lambda repos: ["test_smoke", "test_boot"])
    _patch_no_collected(monkeypatch)
    from otto.cli.test import _tests_completer

    assert _tests_completer(None, "test_") == ["test_boot", "test_smoke"]


def test_tests_completer_unions_collected_over_floor(monkeypatch):
    """A fresh collected set adds dynamic names on top of the static floor."""
    import otto.config as cm
    import otto.config.completion_cache as cc

    _patch_tests_section(monkeypatch, ["test_static"])
    monkeypatch.setattr(cm, "get_repos", list)
    # Collected is fresh (not None) → the warmer must NOT be consulted.
    monkeypatch.setattr(cc, "read_collected_tests", lambda repos: ["test_dynamic"])

    def _boom(repos):
        raise AssertionError("warmer must not run when the collected set is fresh")

    monkeypatch.setattr(cc, "maybe_warm_collected_tests", _boom)
    from otto.cli.test import _tests_completer

    assert _tests_completer(None, "test_") == ["test_dynamic", "test_static"]


def test_tests_completer_warms_on_cold_collected(monkeypatch):
    """A cold collected set triggers one warm; its result enriches this completion."""
    import otto.config as cm
    import otto.config.completion_cache as cc

    _patch_tests_section(monkeypatch, ["test_static"])
    monkeypatch.setattr(cm, "get_repos", list)
    monkeypatch.setattr(cc, "read_collected_tests", lambda repos: None)
    warmed = []

    def _warm(repos):
        warmed.append(True)
        return ["test_generated"]

    monkeypatch.setattr(cc, "maybe_warm_collected_tests", _warm)
    from otto.cli.test import _tests_completer

    assert _tests_completer(None, "test_") == ["test_generated", "test_static"]
    assert warmed == [True]


def _patch_tests_section_with_markers(monkeypatch, names, markers):
    import otto.config.cache_sections as cs

    monkeypatch.setattr(
        cs,
        "read_section",
        lambda repos, name: {"tests": names, "markers": markers} if name == "tests" else None,
    )


def test_markers_completer_unions_collected_over_floor(monkeypatch):
    import otto.config as cm
    import otto.config.completion_cache as cc

    _patch_tests_section_with_markers(monkeypatch, [], ["smoke"])
    monkeypatch.setattr(cm, "get_repos", list)
    monkeypatch.setattr(cc, "read_collected_markers", lambda repos: ["slow"])

    def _boom(repos):
        raise AssertionError("warmer must not run when the collected set is fresh")

    monkeypatch.setattr(cc, "maybe_warm_collected_tests", _boom)
    from otto.cli.test import _markers_completer

    assert _markers_completer(None, "smoke and s") == ["smoke and slow", "smoke and smoke"]


def test_markers_completer_warms_on_cold_collected(monkeypatch):
    import otto.config as cm
    import otto.config.completion_cache as cc

    _patch_tests_section_with_markers(monkeypatch, [], ["smoke"])
    monkeypatch.setattr(cm, "get_repos", list)
    reads = iter([None, ["deep"]])
    monkeypatch.setattr(cc, "read_collected_markers", lambda repos: next(reads))
    warmed = []
    monkeypatch.setattr(cc, "maybe_warm_collected_tests", lambda repos: warmed.append(True))
    from otto.cli.test import _markers_completer

    assert _markers_completer(None, "") == ["deep", "smoke"]
    assert warmed == [True]


def test_markers_completer_falls_back_to_the_live_scan(monkeypatch):
    import otto.config as cm
    import otto.config.cache_sections as cs
    import otto.config.completion_cache as cc

    monkeypatch.setattr(cs, "read_section", lambda repos, name: None)
    monkeypatch.setattr(cm, "get_repos", list)
    monkeypatch.setattr(cc, "collect_marker_names", lambda repos: ["live"])
    monkeypatch.setattr(cc, "read_collected_markers", lambda repos: [])
    from otto.cli.test import _markers_completer

    assert _markers_completer(None, "l") == ["live"]


def test_markers_option_advertises_the_completer():
    import inspect
    from typing import get_args

    from otto.cli import test as test_module

    sig = inspect.signature(test_module.main)  # the callback that declares `markers`
    metadata = get_args(sig.parameters["markers"].annotation)
    option = next(m for m in metadata if hasattr(m, "autocompletion"))
    assert option.autocompletion is test_module._markers_completer
