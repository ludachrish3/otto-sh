"""During completion `otto host <id> <TAB>` offers the host's OWN class's verbs — from the cache."""

from types import SimpleNamespace

import pytest

import otto.cli.expose as expose_module
from otto.cli.expose import HostGroup, cached_host_class_for_id, exposed_cli_names
from otto.host.embedded_host import ZephyrHost
from otto.host.unix_host import UnixHost


def _ctx(host_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(resilient_parsing=True, params={"host_id": host_id}, parent=None)


def test_cached_map_resolves_the_class_without_a_lab(monkeypatch):
    monkeypatch.setattr(
        "otto.registry.get_completion_names",
        lambda: {"host_classes_by_id": {"z1": "zephyr"}},
    )
    assert cached_host_class_for_id("z1") is ZephyrHost


def test_unknown_id_or_unregistered_name_yields_none(monkeypatch):
    monkeypatch.setattr(
        "otto.registry.get_completion_names",
        lambda: {"host_classes_by_id": {"z1": "nosuch"}},
    )
    assert cached_host_class_for_id("z1") is None
    assert cached_host_class_for_id("ghost") is None


def test_falls_back_to_discovery_when_the_cache_is_cold(monkeypatch):
    """A cold cache resolves from DISCOVERED projects — never `get_projects()`, which bootstraps."""
    import otto.config.completion_cache as cc

    monkeypatch.setattr(expose_module, "_discovered_host_classes", None)
    monkeypatch.setattr("otto.registry.get_completion_names", lambda: None)
    monkeypatch.setattr(
        "otto.config.project.discover", lambda: SimpleNamespace(projects=["discovered"])
    )
    monkeypatch.setattr(
        "otto.declared.get_projects",
        lambda: pytest.fail("get_projects() must not run during completion"),
    )
    monkeypatch.setattr(
        cc,
        "collect_host_classes_by_id",
        lambda projects: {"u1": "unix"} if projects == ["discovered"] else {},
    )
    assert cached_host_class_for_id("u1") is UnixHost


def test_cold_cache_collector_runs_once_per_process(monkeypatch):
    """Click resolves every candidate verb through `get_command` in one completion, so a cold
    cache must run the collector at most ONCE per process, not once per verb resolved."""
    import otto.config.completion_cache as cc

    monkeypatch.setattr(expose_module, "_discovered_host_classes", None)
    monkeypatch.setattr("otto.registry.get_completion_names", lambda: None)
    monkeypatch.setattr(
        "otto.config.project.discover", lambda: SimpleNamespace(projects=["discovered"])
    )
    calls: list[list[str]] = []

    def _counting_collect(projects):
        calls.append(projects)
        return {"u1": "unix"}

    monkeypatch.setattr(cc, "collect_host_classes_by_id", _counting_collect)
    assert cached_host_class_for_id("u1") is UnixHost
    assert cached_host_class_for_id("u1") is UnixHost
    assert len(calls) == 1


def test_non_dict_mapping_falls_back_to_discovery_without_raising(monkeypatch):
    """A malformed (non-dict) `host_classes_by_id` value must not raise — fall back to discovery."""
    import otto.config.completion_cache as cc

    monkeypatch.setattr(expose_module, "_discovered_host_classes", None)
    monkeypatch.setattr(
        "otto.registry.get_completion_names",
        lambda: {"host_classes_by_id": ["not", "a", "dict"]},
    )
    monkeypatch.setattr(
        "otto.config.project.discover", lambda: SimpleNamespace(projects=["discovered"])
    )
    monkeypatch.setattr(
        cc,
        "collect_host_classes_by_id",
        lambda projects: {"u1": "unix"} if projects == ["discovered"] else {},
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
    monkeypatch.setattr(
        "otto.registry.get_completion_names", lambda: {"host_classes_by_id": {"z1": 123}}
    )
    assert cached_host_class_for_id("z1") is None
    monkeypatch.setattr(
        "otto.registry.get_completion_names",
        lambda: {"host_classes_by_id": {"z1": ["nested"]}},
    )
    assert cached_host_class_for_id("z1") is None


def test_resilient_menu_is_scoped_to_the_cached_class(monkeypatch):
    monkeypatch.setattr(
        "otto.registry.get_completion_names",
        lambda: {"host_classes_by_id": {"z1": "zephyr"}},
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
    monkeypatch.setattr("otto.registry.get_completion_names", lambda: {"host_classes_by_id": {}})
    group = HostGroup(name="host")
    assert set(group.list_commands(_ctx("ghost"))) == set(group.list_commands(_ctx(None)))


def test_completion_menu_unchanged(monkeypatch, tmp_path):
    """Regression: under completion an out-of-scope target's menu is its class's, unadmitted."""
    from tests._fixtures.fleet import scope_workspace

    ws = scope_workspace(tmp_path)
    assert "a-jump" not in ws.ctx.targetable_ids()
    monkeypatch.setattr(
        "otto.registry.get_completion_names",
        lambda: {"host_classes_by_id": {"a-jump": "unix"}},
    )
    group = HostGroup(name="host")

    names = group.list_commands(_ctx("a-jump"))

    assert names == group.scoped_names(_ctx("a-jump"), UnixHost)
    assert "exec" in names


def test_completion_never_admits_or_opens_context(monkeypatch):
    """Regression: completion answers from the cached class and never opens a context.

    With the lab probe, the context builder, the context lookups and help
    admission all raising, the menu is still the cached class's and a verb
    still resolves.
    """

    def _explode(*_args, **_kwargs):
        raise AssertionError("completion opened or read a context")

    monkeypatch.setattr(
        "otto.registry.get_completion_names",
        lambda: {"host_classes_by_id": {"z1": "zephyr"}},
    )
    monkeypatch.setattr("otto.cli.invoke.try_ensure_lab", _explode)
    monkeypatch.setattr("otto.run_context.new_run_context", _explode)
    monkeypatch.setattr("otto.run_context.get_context", _explode)
    monkeypatch.setattr("otto.run_context.try_get_context", _explode)
    # Admission itself: a menu that admitted under completion would call it.
    monkeypatch.setattr("otto.cli.host_lookup.help_refusal", _explode)
    group = HostGroup(name="host")
    verb = min(exposed_cli_names(ZephyrHost))

    names = group.list_commands(_ctx("z1"))

    assert set(names) <= exposed_cli_names(ZephyrHost) | (
        set(group.list_commands(_ctx(None))) - group._dynamic_names
    )
    assert verb in names
    assert group.get_command(_ctx("z1"), verb) is not None
