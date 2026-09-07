"""Unit tests for the Mount record and its prefix matching."""

import dataclasses
from pathlib import Path

import pytest

from otto.host.mount import Mount, mount_for, mount_for_parent, translate

APP = Mount(container_path=Path("/var/lib/app"), parent_path=Path("/srv/data"), kind="bind")
LOGS = Mount(container_path=Path("/var/lib/app/logs"), parent_path=Path("/srv/logs"), kind="bind")
INNER_DATA = Mount(
    container_path=Path("/var/lib/inner"),
    parent_path=Path("/srv/data/inner"),
    kind="bind",
)
VOL = Mount(
    container_path=Path("/data"),
    parent_path=Path("/var/lib/docker/volumes/appvol/_data"),
    kind="volume",
    name="appvol",
)


def test_mount_for_exact_match():
    assert mount_for([APP], "/var/lib/app") is APP


def test_mount_for_sub_path():
    assert mount_for([APP], "/var/lib/app/logs/x.txt") is APP


def test_mount_for_prefers_the_longest_prefix():
    # LOGS nests inside APP. First-match would answer APP and name a parent
    # path where the file does not exist.
    assert mount_for([APP, LOGS], "/var/lib/app/logs/x.txt") is LOGS
    assert mount_for([LOGS, APP], "/var/lib/app/logs/x.txt") is LOGS


def test_mount_for_matches_components_not_string_prefixes():
    assert mount_for([APP], "/var/lib/application/x") is None


def test_mount_for_no_match_returns_none():
    assert mount_for([APP], "/etc/hosts") is None


def test_mount_for_empty_list_returns_none():
    assert mount_for([], "/var/lib/app") is None


def test_mount_for_parent_matches_on_the_parent_side():
    assert mount_for_parent([APP, VOL], "/srv/data/logs/x.txt") is APP
    assert mount_for_parent([APP, VOL], "/var/lib/docker/volumes/appvol/_data/x") is VOL


def test_mount_for_parent_prefers_the_longest_prefix():
    # INNER_DATA nests inside APP on the parent side. Longest prefix should win.
    assert mount_for_parent([APP, INNER_DATA], "/srv/data/inner/x") is INNER_DATA
    assert mount_for_parent([INNER_DATA, APP], "/srv/data/inner/x") is INNER_DATA


def test_translate_to_parent_carries_the_remainder():
    assert translate(APP, "/var/lib/app/logs/x.txt", to_parent=True) == Path("/srv/data/logs/x.txt")


def test_translate_to_parent_on_the_mount_root():
    assert translate(APP, "/var/lib/app", to_parent=True) == Path("/srv/data")


def test_translate_to_container_carries_the_remainder():
    assert translate(APP, "/srv/data/logs/x.txt", to_parent=False) == Path(
        "/var/lib/app/logs/x.txt"
    )


def test_translate_accepts_a_path_object():
    assert translate(APP, Path("/var/lib/app/x"), to_parent=True) == Path("/srv/data/x")


def test_translate_refuses_a_path_outside_the_mount():
    with pytest.raises(ValueError, match="is not in the subpath of"):
        translate(APP, "/etc/hosts", to_parent=True)


def test_read_only_defaults_false_and_is_settable():
    assert APP.read_only is False
    assert Mount(Path("/a"), Path("/b"), "bind", read_only=True).read_only is True


def test_mount_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        APP.kind = "volume"  # type: ignore[misc]
