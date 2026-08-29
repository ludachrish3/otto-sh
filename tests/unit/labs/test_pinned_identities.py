"""The fixtures build the hosts their pre-v2 baseline built (spec
2026-08-27 lab-definition-v2 §13 'Flattening').

Pinned literals recorded from the v1 world before the switch — a v1 file
cannot be loaded after the break, so equivalence is pinned, not computed.
Re-deriving these from the migrated files would make the test circular: it
would then assert only that the loader agrees with itself.

Each tuple is ``(host.id, host.element, host.element_id, host.logical_index)``
for every non-``local`` host of the lab, sorted. Those four values are the
whole identity contract the v2 flattening had to preserve: ``make_host_id``,
``slug`` and ``logical_indices`` were not touched by this change, so any drift
here means the element→host flattening lost or invented a field.
"""

import pytest

from otto.config.lab import load_lab
from otto.host.builtin_hosts import BUILTIN_LOCAL_HOST_ID
from otto.host.command_frame import FRAME_CLASSES, register_command_frame
from tests._fixtures.labdata import lab_data_dir
from tests._fixtures.paths import ensure_custom_hosts_on_path

ensure_custom_hosts_on_path()


@pytest.fixture(autouse=True)
def _zephyr_inline_frame() -> None:
    """Register the frame ``tech1``'s ``zephyr27_fat`` declares.

    ``command_frame: "zephyr-inline"`` lives in the shared ``custom_hosts``
    package, which a real repo pulls in through ``settings.toml``'s ``init``.
    This suite loads the fixture lab directly (no repo), so nothing imports it
    — and the ``embedded`` lab would fail to build. Registered explicitly
    rather than by importing ``custom_hosts`` for its side effect: the root
    conftest's registry isolation can leave that package in ``sys.modules``
    with its entry already unregistered, and a second import would then be a
    no-op that registers nothing.
    """
    from custom_hosts.zephyr_inline import ZephyrInlineRetcodeFrame

    if ZephyrInlineRetcodeFrame.type_name not in FRAME_CLASSES:
        register_command_frame(ZephyrInlineRetcodeFrame.type_name, ZephyrInlineRetcodeFrame)


# (tech, lab) -> the identity tuples recorded at the v1 baseline.
_PINNED: dict[tuple[str, str], list[tuple[str, str, int | None, int | None]]] = {
    ("tech1", "unix"): [
        ("test1", "test1", None, None),
        ("test2", "test2", None, None),
        ("test3", "test3", None, None),
    ],
    ("tech1", "busybox"): [
        ("bb1161_qemu", "bb1161", None, None),
        ("bb1211_qemu", "bb1211", None, None),
        ("bb1281_qemu", "bb1281", None, None),
        ("bb1310_qemu", "bb1310", None, None),
        ("bb1350_qemu", "bb1350", None, None),
        ("test1", "test1", None, None),
    ],
    ("tech1", "embedded"): [
        ("test4", "test4", None, None),
        ("zephyr27-fat", "zephyr27_fat", None, None),
        ("zephyr37-fat", "zephyr37_fat", None, None),
        ("zephyr37-lfs", "zephyr37_lfs", None, None),
        ("zephyr37-llext", "zephyr37_llext", None, None),
        ("zephyr37-nofs", "zephyr37_nofs", None, None),
        ("zephyr44-lfs", "zephyr44_lfs", None, None),
        ("zephyr44-llext", "zephyr44_llext", None, None),
    ],
    ("tech2", "unix_alt"): [
        ("alt1", "alt1", None, None),
        ("alt2", "alt2", None, None),
        ("alt3", "alt3", None, None),
    ],
}

# (tech, lab) -> the per-lab reservation set recorded at the v1 baseline, when
# it was the UNION of the member hosts' ``resources``. In v2 it is the lab
# entry's declared ``resources``; the migration had to carry the same set over.
_PINNED_RESOURCES: dict[tuple[str, str], set[str]] = {
    ("tech1", "unix"): {"test1", "test2", "test3"},
    ("tech1", "busybox"): {"bb1161", "bb1211", "bb1281", "bb1310", "bb1350", "test1"},
    ("tech1", "embedded"): {
        "test4",
        "zephyr27_fat",
        "zephyr37_fat",
        "zephyr37_lfs",
        "zephyr37_llext",
        "zephyr37_nofs",
        "zephyr44_lfs",
        "zephyr44_llext",
    },
    ("tech2", "unix_alt"): {"alt1", "alt2", "alt3"},
}

# Element- and host-level sets (spec 2026-08-28 three-level-reservations §11):
# the lab-level pins above are UNCHANGED by that spec; these are new pins.
#
# EVERY host of each pinned lab is listed, empties included. A partial mapping
# pins only what it names, so a stray `resources` added to any host the map
# left out would pass — and the empties are the half of this pin that says the
# spec's two new levels did not leak into the rest of the fixtures.
_PINNED_ELEMENT_RESOURCES: dict[tuple[str, str], dict[str, set[str]]] = {
    ("tech1", "unix"): {"test1": {"test1-chassis"}, "test2": set(), "test3": set()},
    ("tech1", "busybox"): {
        "bb1161_qemu": set(),
        "bb1211_qemu": set(),
        "bb1281_qemu": set(),
        "bb1310_qemu": set(),
        "bb1350_qemu": set(),
        "test1": {"test1-chassis"},
    },
}
_PINNED_HOST_RESOURCES: dict[tuple[str, str], dict[str, set[str]]] = {
    ("tech1", "unix"): {"test1": set(), "test2": {"test2-console"}, "test3": set()},
    ("tech1", "busybox"): {
        "bb1161_qemu": set(),
        "bb1211_qemu": set(),
        "bb1281_qemu": set(),
        "bb1310_qemu": set(),
        "bb1350_qemu": set(),
        "test1": set(),
    },
}


@pytest.mark.parametrize(("tech", "lab"), sorted(_PINNED))
def test_identities_unchanged(tech: str, lab: str) -> None:
    """Every built host keeps the id, element, element id and logical index it had on v1."""
    built = load_lab(lab, search_paths=[lab_data_dir() / tech])
    got = sorted(
        (h.id, h.element, h.element_id, h.logical_index)
        for h in built.hosts.values()
        if h.id != "local"
    )
    assert got == _PINNED[(tech, lab)]


@pytest.mark.parametrize(("tech", "lab"), sorted(_PINNED_RESOURCES))
def test_reservation_sets_match_the_old_host_union(tech: str, lab: str) -> None:
    """The declared ``labs`` entry reserves exactly what the v1 hosts' union did."""
    built = load_lab(lab, search_paths=[lab_data_dir() / tech])
    assert built.resources == _PINNED_RESOURCES[(tech, lab)]


def _pinned_every_host(built, mapping: dict[str, set[str]]) -> None:
    """Assert *mapping* names every non-``local`` host of *built*, and no other.

    Without this the maps pin only what they happen to name: a host added to a
    pinned lab later would carry any ``resources`` at all and both tests below
    would stay green, because they iterate the MAP and not the LAB. ``local`` is
    excluded because the loader injects it into every lab — it is not lab data,
    and it can never declare a reservation identifier.
    """
    assert set(mapping) == set(built.hosts) - {BUILTIN_LOCAL_HOST_ID}, sorted(
        set(built.hosts) - {BUILTIN_LOCAL_HOST_ID} - set(mapping)
    )


@pytest.mark.parametrize(("tech", "lab"), sorted(_PINNED_ELEMENT_RESOURCES))
def test_element_resources_pinned(tech: str, lab: str) -> None:
    """Every built host carries its element's declared ``resources``."""
    built = load_lab(lab, search_paths=[lab_data_dir() / tech])
    mapping = _PINNED_ELEMENT_RESOURCES[(tech, lab)]
    _pinned_every_host(built, mapping)
    for host_id, expected in mapping.items():
        assert built.hosts[host_id].element_resources == frozenset(expected), host_id


@pytest.mark.parametrize(("tech", "lab"), sorted(_PINNED_HOST_RESOURCES))
def test_host_resources_pinned(tech: str, lab: str) -> None:
    """Every built host carries the ``resources`` its own host entry declared."""
    built = load_lab(lab, search_paths=[lab_data_dir() / tech])
    mapping = _PINNED_HOST_RESOURCES[(tech, lab)]
    _pinned_every_host(built, mapping)
    for host_id, expected in mapping.items():
        assert built.hosts[host_id].resources == frozenset(expected), host_id
