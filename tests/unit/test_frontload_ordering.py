"""Heavy serial xdist_group items are collected ahead of the parallel pool.

``_FRONTLOAD_GROUPS`` and ``_frontload_key`` are pure helpers defined in the
root conftest; they are tested here without spinning up a VM (no live markers
needed).

Validated by the Phase-3 spike (docs/superpowers/specs/2026-06-23-frontload-spike-findings.md):
collection order is the xdist LoadGroupScheduling dispatch order.
"""

import pytest


def test_frontload_key_heavy_groups_return_zero():
    """All named heavy groups map to priority 0."""
    from tests.conftest import _frontload_key

    assert _frontload_key("sprout_cov") == 0
    assert _frontload_key("docker_e2e") == 0
    assert _frontload_key("coverage_e2e") == 0
    assert _frontload_key("zephyr_fanout") == 0


def test_frontload_key_none_returns_one():
    """Items with no xdist_group marker (group=None) map to priority 1."""
    from tests.conftest import _frontload_key

    assert _frontload_key(None) == 1


def test_frontload_key_arbitrary_group_returns_one():
    """An ordinary (non-heavy) xdist_group string maps to priority 1."""
    from tests.conftest import _frontload_key

    assert _frontload_key("unit_thing") == 1
    assert _frontload_key("some_other_group") == 1


def test_frontload_key_heavy_before_plain():
    """Heavy group sorts strictly before non-heavy."""
    from tests.conftest import _frontload_key

    assert _frontload_key("docker_e2e") < _frontload_key("unit_thing")
    assert _frontload_key("zephyr_fanout") < _frontload_key(None)


def test_sort_puts_heavy_item_first():
    """list.sort with _frontload_key moves heavy items to the front.

    Uses fake items to stay VM-free — only the group name extraction logic
    matters for this unit.
    """
    from tests.conftest import _frontload_key

    class _FakeItem:
        def __init__(self, group: str | None) -> None:
            self._group = group

        def get_closest_marker(self, name: str):
            if name == "xdist_group" and self._group is not None:
                class _M:
                    args = (self._group,)
                return _M()
            return None

    def group_of(item):
        m = item.get_closest_marker("xdist_group")
        return m.args[0] if (m and m.args) else None

    items = [
        _FakeItem("unit_thing"),   # plain — should sort to back
        _FakeItem("docker_e2e"),   # heavy — should sort to front
        _FakeItem(None),           # plain (no marker) — should sort to back
        _FakeItem("sprout_cov"),   # heavy — should sort to front
        _FakeItem("other_group"),  # plain — should sort to back
        _FakeItem("zephyr_fanout"),# heavy — should sort to front
    ]

    items.sort(key=lambda it: _frontload_key(group_of(it)))

    # All heavy items should come before all plain items.
    groups = [group_of(it) for it in items]
    heavy_groups = {"sprout_cov", "docker_e2e", "coverage_e2e", "zephyr_fanout"}
    first_plain_idx = next(
        (i for i, g in enumerate(groups) if g not in heavy_groups), len(groups)
    )
    last_heavy_idx = max(
        (i for i, g in enumerate(groups) if g in heavy_groups), default=-1
    )
    assert last_heavy_idx < first_plain_idx, (
        f"Heavy items did not all precede plain items. Order: {groups}"
    )


def test_frontload_groups_contains_expected_names():
    """_FRONTLOAD_GROUPS contains exactly the four validated heavy groups."""
    from tests.conftest import _FRONTLOAD_GROUPS

    assert _FRONTLOAD_GROUPS == frozenset(
        {"sprout_cov", "docker_e2e", "coverage_e2e", "zephyr_fanout"}
    )
