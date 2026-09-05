"""During completion `otto host <id> <TAB>` offers the host's OWN class's verbs — from the cache."""

from types import SimpleNamespace

import pytest

import otto.cli.expose as expose_module
import otto.config as cm
from otto.cli.expose import HostGroup, cached_host_class_for_id, exposed_cli_names
from otto.host.embedded_host import ZephyrHost
from otto.host.unix_host import UnixHost


def _ctx(host_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(resilient_parsing=True, params={"host_id": host_id}, parent=None)


def test_cached_map_resolves_the_class_without_a_lab(monkeypatch):
    monkeypatch.setattr(
        cm, "get_completion_names", lambda: {"host_classes_by_id": {"z1": "zephyr"}}
    )
    assert cached_host_class_for_id("z1") is ZephyrHost


def test_unknown_id_or_unregistered_name_yields_none(monkeypatch):
    monkeypatch.setattr(
        cm, "get_completion_names", lambda: {"host_classes_by_id": {"z1": "nosuch"}}
    )
    assert cached_host_class_for_id("z1") is None
    assert cached_host_class_for_id("ghost") is None


def test_falls_back_to_discovery_when_the_cache_is_cold(monkeypatch):
    """A cold cache resolves from DISCOVERED repos — never `get_repos()`, which would bootstrap."""
    import otto.bootstrap as bs
    import otto.config.completion_cache as cc

    monkeypatch.setattr(expose_module, "_discovered_host_classes", None)
    monkeypatch.setattr(cm, "get_completion_names", lambda: None)
    monkeypatch.setattr(bs, "discover", lambda: SimpleNamespace(repos=["discovered"]))
    monkeypatch.setattr(
        cm, "get_repos", lambda: pytest.fail("get_repos() must not run during completion")
    )
    monkeypatch.setattr(
        cc,
        "collect_host_classes_by_id",
        lambda repos: {"u1": "unix"} if repos == ["discovered"] else {},
    )
    assert cached_host_class_for_id("u1") is UnixHost


def test_cold_cache_collector_runs_once_per_process(monkeypatch):
    """Click resolves every candidate verb through `get_command` in one completion, so a cold
    cache must run the collector at most ONCE per process, not once per verb resolved."""
    import otto.bootstrap as bs
    import otto.config.completion_cache as cc

    monkeypatch.setattr(expose_module, "_discovered_host_classes", None)
    monkeypatch.setattr(cm, "get_completion_names", lambda: None)
    monkeypatch.setattr(bs, "discover", lambda: SimpleNamespace(repos=["discovered"]))
    calls: list[list[str]] = []

    def _counting_collect(repos):
        calls.append(repos)
        return {"u1": "unix"}

    monkeypatch.setattr(cc, "collect_host_classes_by_id", _counting_collect)
    assert cached_host_class_for_id("u1") is UnixHost
    assert cached_host_class_for_id("u1") is UnixHost
    assert len(calls) == 1


def test_non_dict_mapping_falls_back_to_discovery_without_raising(monkeypatch):
    """A malformed (non-dict) `host_classes_by_id` value must not raise — fall back to discovery."""
    import otto.bootstrap as bs
    import otto.config.completion_cache as cc

    monkeypatch.setattr(expose_module, "_discovered_host_classes", None)
    monkeypatch.setattr(
        cm, "get_completion_names", lambda: {"host_classes_by_id": ["not", "a", "dict"]}
    )
    monkeypatch.setattr(bs, "discover", lambda: SimpleNamespace(repos=["discovered"]))
    monkeypatch.setattr(
        cc,
        "collect_host_classes_by_id",
        lambda repos: {"u1": "unix"} if repos == ["discovered"] else {},
    )
    assert cached_host_class_for_id("u1") is UnixHost


def test_non_str_class_name_yields_none(monkeypatch):
    """A malformed (non-str) class name must not raise and must yield ``None``.

    ``123`` alone would pass even without the guard — ``get_host_class`` calls
    ``name in HOST_CLASSES`` (a ``dict``), and a hashable, unregistered key like
    an int just misses cleanly. An UNHASHABLE value (a ``list``) is the case
    that actually falls over without the guard: ``in`` on a ``dict`` raises
    ``TypeError: unhashable type`` for it instead of returning ``False``. Both
    are asserted so the guard is proven load-bearing, not just documented.
    """
    monkeypatch.setattr(cm, "get_completion_names", lambda: {"host_classes_by_id": {"z1": 123}})
    assert cached_host_class_for_id("z1") is None
    monkeypatch.setattr(
        cm, "get_completion_names", lambda: {"host_classes_by_id": {"z1": ["nested"]}}
    )
    assert cached_host_class_for_id("z1") is None


def test_resilient_menu_is_scoped_to_the_cached_class(monkeypatch):
    monkeypatch.setattr(
        cm, "get_completion_names", lambda: {"host_classes_by_id": {"z1": "zephyr"}}
    )
    group = HostGroup(name="host")
    names = set(group.list_commands(_ctx("z1")))
    assert (
        names
        <= exposed_cli_names(ZephyrHost)
        | set(group.list_commands(_ctx(None))) - group._dynamic_names
    )
    assert not (names & (exposed_cli_names(UnixHost) - exposed_cli_names(ZephyrHost)))


def test_resilient_menu_is_the_union_for_an_unknown_id(monkeypatch):
    monkeypatch.setattr(cm, "get_completion_names", lambda: {"host_classes_by_id": {}})
    group = HostGroup(name="host")
    assert set(group.list_commands(_ctx("ghost"))) == set(group.list_commands(_ctx(None)))
