"""The axis resolver must inherit otto's defaulting, never restate it."""

import sys

import pytest

from tests._fixtures.labdata import flat_hosts
from tests._fixtures.profiles import Cell, _entries, _userland, axes_for, axis_space


def test_userland_reads_gnu_for_a_plain_unix_host() -> None:
    assert _userland({"os_type": "unix"}) == "gnu"


def test_userland_reads_busybox_from_the_userland_layer_not_from_os_type() -> None:
    """BusyBox guests report ``os_type: unix`` — the flavor is NOT readable off it."""
    data = {"os_type": "unix", "sw_version": "1.35.0", "userland_options": {"shell_dialect": "ash"}}
    assert _userland(data) == "busybox-1.35.0"


def test_userland_reads_zephyr_with_its_version() -> None:
    assert (
        _userland({"os_family": "embedded", "os_type": "zephyr", "os_version": "3.7"})
        == "zephyr-3.7"
    )


@pytest.mark.parametrize(
    "data",
    [
        {"os_family": "embedded", "os_type": "zephyr"},
        {"os_type": "unix", "userland_options": {"shell_dialect": "ash"}},
    ],
    ids=["zephyr-without-os_version", "busybox-without-sw_version"],
)
def test_userland_raises_rather_than_inventing_a_version(data: dict) -> None:
    """A placeholder version is how a wrong axis value reaches a matrix cell."""
    with pytest.raises(ValueError, match="version"):
        _userland(data)


def test_userland_keys_on_family_not_on_the_profile_name() -> None:
    """`os_type` is a profile SELECTOR; the factory picks the class from
    `profile.base` (factory.py:154-157), so the name and the family are
    different layers. repo1 ships profiles named `zephyr-3.7`/`-2.7`/`-4.4`,
    all `base = "embedded"`. No bed host trips this today -- every Zephyr
    guest declares `os_type: "zephyr"` -- so it is pinned before one does.
    """
    embedded = {"os_family": "embedded", "os_type": "zephyr-3.7", "os_version": "3.7"}
    assert _userland(embedded) == "zephyr-3.7"

    # The negative half is the point: a zephyr-LOOKING name on a unix base
    # must NOT resolve to zephyr. A `startswith("zephyr")` check would.
    unixish = {"os_family": "unix", "os_type": "zephyr-3.7", "sw_version": "9"}
    assert _userland(unixish) == "gnu"


def test_axes_for_a_host_that_declares_everything() -> None:
    axes = axes_for("test1")
    assert axes.os_type == "unix"
    assert axes.userland == "gnu"
    assert axes.terms == ["ssh", "telnet"]
    assert axes.docker_capable is True
    assert axes.hop_depth == 0


def test_axes_for_a_host_that_declares_no_menus_at_all() -> None:
    """alt1 has no os_type, no valid_terms and no valid_transfers in lab.json.

    A resolver reading the raw file would return empty menus here. The factory
    supplies them, which is the whole reason this module builds the host.
    """
    axes = axes_for("alt1", tech="tech2")
    assert axes.os_type == "unix"
    assert axes.terms == ["ssh", "telnet"]
    assert axes.transfers == ["scp", "sftp", "ftp", "nc"]


def test_axes_for_a_zephyr_guest_gets_terms_it_never_declared() -> None:
    axes = axes_for("zephyr37_fat")
    assert axes.userland == "zephyr-3.7"
    assert axes.terms == ["telnet"]
    assert axes.transfers == ["console"]
    assert axes.hop_depth == 1


def test_axes_for_a_busybox_guest_is_not_gnu_despite_os_type_unix() -> None:
    """This pins the userland flavor only; bb1350 declares all its menus, so
    it does NOT discriminate raw-vs-host reading. The alt1 and zephyr37_fat
    tests carry that."""
    axes = axes_for("bb1350")
    assert axes.os_type == "unix"
    assert axes.userland == "busybox-1.35.0"
    assert axes.hop_depth == 1
    assert axes.docker_capable is False


def test_an_empty_userland_table_is_not_a_busybox_claim() -> None:
    """An empty table declares nothing, so it must not assert a flavor.

    Pinned because truthiness and presence are indistinguishable against the
    current fixtures: without this, ``"userland_options" in data`` would pass
    every other test while meaning something different.
    """
    assert _userland({"os_type": "unix", "userland_options": {}}) == "gnu"


def test_axes_for_a_guest_whose_frame_lives_out_of_tree() -> None:
    """zephyr27_fat's command_frame is registered by tests/custom_hosts, not
    by otto. Resolving it must not depend on some other conftest having
    imported that module first.

    This alone is a WEAK guard against the registry-eviction hazard: under
    the naive pre-fix `_ensure_custom_frames`, whether this goes red depends
    on pytest-randomly's ordering -- it needs some other `axes_for` call to
    run first on the same worker.
    `test_axes_for_recovers_when_registry_isolation_has_dropped_the_frame`
    injects that condition directly and is the deterministic guard; keep
    this one for the plain "can we resolve this host at all" case.
    """
    axes = axes_for("zephyr27_fat")
    assert axes.userland == "zephyr-2.7"
    assert axes.terms == ["telnet"]


def test_axes_for_recovers_when_registry_isolation_has_dropped_the_frame(monkeypatch) -> None:
    """Reproduce `_restore_registries`' teardown inline, so this does not
    depend on which test pytest-randomly happened to run first.

    `_restore_registries` (tests/conftest.py:1766-1837, called from
    `_isolate_registries`'s teardown at tests/conftest.py:1711) drops a
    test-added registry entry and evicts `reg.origin(name)` from
    `sys.modules`. For `zephyr-inline` that origin is `custom_hosts` -- the
    `__init__.py` that calls `register_command_frame` -- NOT the
    `custom_hosts.zephyr_inline` submodule, which stays cached. A plain
    `import_module` on the dotted name then hits Python's cached-module fast
    path, never re-runs `__init__.py`, and the frame stays unregistered.
    Measured: unlike `test_axes_for_a_guest_whose_frame_lives_out_of_tree`
    (which passed on the default run under the naive implementation and so
    would have certified it as working), this test -- because it constructs
    the hostile condition in its own body -- fails on every ordering,
    including the default run, not just seeds 1 and 4.
    """
    from otto.host.command_frame import FRAME_CLASSES

    axes_for("zephyr27_fat")  # register + import
    FRAME_CLASSES.unregister("zephyr-inline")  # what the teardown drops
    monkeypatch.delitem(sys.modules, "custom_hosts", raising=False)  # what the teardown evicts
    assert "custom_hosts.zephyr_inline" in sys.modules, (
        "premise gone: the teardown no longer leaves a dangling submodule, "
        "so this test is not reproducing the hazard it names"
    )

    axes = axes_for("zephyr27_fat")  # must recover unaided
    assert axes.userland == "zephyr-2.7"


def test_docker_capable_comes_from_the_host_not_the_raw_entry(monkeypatch) -> None:
    """A raw JSON string "false" is truthy; pydantic coerces it to False.

    Routed through `axes_for` DELIBERATELY. An earlier version of this test
    called `create_host_from_dict` directly and passed against
    `bool(data.get("docker_capable", False))` -- it pinned pydantic, not
    this module's choice, and left the choice unfalsifiable.
    """
    from tests._fixtures import profiles as profiles_mod

    real = profiles_mod._entries("tech1")
    doctored = dict(real)
    doctored["test4"] = dict(real["test4"]) | {"docker_capable": "false"}
    monkeypatch.setattr(profiles_mod, "_entries", lambda tech="tech1": doctored)

    assert bool(doctored["test4"]["docker_capable"]) is True  # raw: truthy string
    assert axes_for("test4").docker_capable is False  # via the host: False


def test_os_family_comes_from_the_host_class_not_the_os_type_string(monkeypatch) -> None:
    """A profile whose NAME is not "zephyr" but whose base IS embedded.

    ``os_type`` is a profile selector; the factory picks the class from
    ``profile.base`` (factory.py:154-157). No bed host selects a renamed
    profile, so one is registered here -- without it the ``isinstance`` check
    in ``axes_for`` is exercised by every Zephyr guest but falsified by none
    of them. Measured: with this test absent, replacing that check with
    ``host.os_type == "zephyr"`` keeps all 38 other tests in this file green,
    so the module's central derivation was unpinned at the layer that
    performs it (``test_userland_keys_on_family_not_on_the_profile_name``
    pins the same argument, but only against hand-built dicts).

    ``userland`` stays ``zephyr-3.7`` rather than becoming ``zephyr-9.9``:
    the entry's own ``os_version`` wins over the profile default, measured.
    The load-bearing part is the ``zephyr-`` prefix, which a string
    comparison on ``os_type`` turns into ``gnu``.
    """
    from otto.host.os_profile import register_os_profile
    from tests._fixtures import profiles as profiles_mod

    register_os_profile(
        "zephyr-9.9",
        base="embedded",
        defaults={
            "os_name": "Zephyr",
            "os_version": "9.9",
            "command_frame": "zephyr",
            "transfer": "console",
        },
    )
    real = profiles_mod._entries("tech1")
    doctored = dict(real)
    doctored["zephyr37_fat"] = dict(real["zephyr37_fat"]) | {"os_type": "zephyr-9.9"}
    monkeypatch.setattr(profiles_mod, "_entries", lambda tech="tech1": doctored)

    axes = axes_for("zephyr37_fat")
    assert axes.os_type == "zephyr-9.9"  # the profile NAME, which is not "zephyr"
    assert axes.userland == "zephyr-3.7"  # the FAMILY, read off the host class


def test_axis_space_cells_come_from_the_built_host() -> None:
    """The cells must be built from a host otto built, not from the raw entry.

    Measured: ``zephyr37_fat`` declares ``valid_transfers: ["console"]`` in
    lab.json but NO ``valid_terms`` at all -- the factory supplies the terms.
    So a cell list re-derived from the raw entry crosses ``[]`` with
    ``["console"]`` and comes out EMPTY, and only a built host yields the
    ``telnet``/``console`` cell below. Measured again: re-deriving the menus
    from the raw entry fails this test and no other in this file, because
    every host in the ``unix`` lab does declare both of its menus.

    This test does NOT guard the crossing, and must not be read as if it did.
    At 1 term x 1 transfer a cross and a non-cross produce the identical
    list, so an implementation that paired the menus instead of crossing them
    would keep this green.
    ``test_axis_space_crosses_the_two_menus_rather_than_pairing_them`` is the
    guard for that, and it uses a host with more than one entry in both menus
    for exactly that reason.

    Filtered to one element on purpose: the ``embedded`` lab also holds
    ``test4``, a unix host (measured: 2 terms x 4 transfers = 8 cells), so
    asserting the whole lab here would assert two unrelated things at once.
    """
    cells = axis_space("embedded")
    zephyr = [c for c in cells if c.element == "zephyr37_fat"]
    assert zephyr == [Cell("zephyr37_fat", "telnet", "console")]


def test_axis_space_crosses_the_two_menus_rather_than_pairing_them() -> None:
    """test1 has 2 terms and 4 transfers, so a cross gives 8 cells and any
    pairing scheme gives fewer.

    Deliberately NOT zephyr37_fat: that guest is 1 term x 1 transfer, where a
    cross and a non-cross produce the identical list, so it cannot falsify
    this claim. Measured: an implementation pinning ``transfers[0]`` yields 2
    cells here and this assertion fails -- and it does NOT fail alone.
    ``test_axis_space_covers_every_host_in_the_lab`` fails with it (it
    expects 24 cells and gets 6). Measured: those two are the only tests in
    this file that detect the pin. In particular
    ``test_axis_space_cells_come_from_the_built_host`` above stays green,
    because at 1 term x 1 transfer it cannot see the difference -- which is
    why this test exists separately from it.

    A set, not a list: menu ORDER is the host's own answer and differs
    between hosts (measured: ``test2`` reports ``['telnet', 'ssh']`` while
    ``test1`` and ``test3`` report ``['ssh', 'telnet']``), so asserting an
    order here would pin something this test is not about.
    """
    cells = {c for c in axis_space("unix") if c.element == "test1"}
    assert cells == {
        Cell("test1", term, transfer)
        for term in ("ssh", "telnet")
        for transfer in ("scp", "sftp", "ftp", "nc")
    }


def test_axis_space_covers_every_host_in_the_lab() -> None:
    """A shrinking space must be visible, so the count is asserted, not sampled."""
    cells = axis_space("unix")
    assert {c.element for c in cells} == {"test1", "test2", "test3"}
    assert len(cells) == 3 * 2 * 4  # three hosts x two terms x four transfers


def test_axis_space_rejects_a_lab_no_host_declares() -> None:
    """An empty space is the failure mode this raise exists to prevent: it
    satisfies every sampling assertion vacuously, so a typo'd lab name would
    read as full conformance rather than as no coverage at all.
    """
    with pytest.raises(KeyError, match="veggies"):
        axis_space("veggies")


def _all_hosts() -> list[tuple[str, str]]:
    """Every ``(tech, element)`` in the bed, read straight from the lab files.

    Enumerated through the shared lab-data flattener rather than through
    ``_entries``: the population under test must not be supplied by the module
    under test, or a resolver that lost hosts would silently shrink its own
    guard instead of failing it. ``flat_hosts`` is still an independent route
    -- it lives in ``tests/_fixtures/labdata.py`` and knows nothing about
    ``profiles`` -- it just spells the v2 element walk once instead of here.
    The test below still calls ``_entries`` for the raw entry of a host it was
    already given, which carries no such hazard -- a missing host raises
    ``KeyError`` there rather than quietly going uncovered.
    """
    return [(tech, host["element"]) for tech in ("tech1", "tech2") for host in flat_hosts(tech)]


@pytest.mark.parametrize(("tech", "element"), _all_hosts())
def test_axes_match_the_host_otto_actually_builds(tech: str, element: str) -> None:
    """Every host's axes must be the ones the factory resolved -- no exceptions.

    All nineteen rather than a sample, because the bed splits into two halves
    that fail in OPPOSITE directions and only the whole population covers
    both. Measured against the current lab data:

    * Ten hosts omit ``valid_terms`` -- the seven Zephyr guests (which do
      declare ``valid_transfers: ["console"]``, so "declares no menus" would
      be the wrong description) and alt1/alt2/alt3 (which declare no
      ``os_type`` and neither menu). The factory supplies the term menu for
      all ten, so a resolver that went back to reading the raw file hands
      them an empty one and the non-empty assertions below go red.
    * The other nine -- test1, test2, test3, test4 and bb1161/bb1211/bb1281/
      bb1310/bb1350 -- declare ``valid_terms``, and measured, ``axes_for``
      returns each declared list unchanged. They are the half that catches
      the opposite mistake: a resolver that OVERRODE a menu the lab data had
      already stated. Those nine stay green under a raw read, which is
      exactly why sampling them would certify nothing.
    """
    axes = axes_for(element, tech)
    assert axes.terms, f"{element}: resolved an empty term menu"
    assert axes.transfers, f"{element}: resolved an empty transfer menu"

    entry = _entries(tech)[element]
    if "valid_terms" in entry:
        assert axes.terms == entry["valid_terms"], (
            f"{element}: the factory overrode a menu the lab data declared"
        )


def test_at_least_ten_hosts_still_omit_their_term_menu() -> None:
    """Pin the premise the guard above rests on.

    If the lab data ever starts declaring ``valid_terms`` everywhere, that
    guard stops discriminating a raw read from a host read -- every case would
    pass either way -- and it should be re-pointed at whatever the factory
    still defaults. Measured today: ten of the nineteen omit it, listed in the
    failure message so the shrink is readable rather than inferred.
    """
    undeclared = [
        host["element"]
        for tech in ("tech1", "tech2")
        for host in flat_hosts(tech)
        if "valid_terms" not in host
    ]
    assert len(undeclared) >= 10, (
        f"only {len(undeclared)} hosts omit valid_terms ({undeclared}); this "
        f"guard exists because the factory fills in the term menu for hosts "
        f"that do not declare one"
    )
