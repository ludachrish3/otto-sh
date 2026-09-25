"""Proven-range data and comparison (spec 2026-09-24 §3.4)."""

import pytest

from otto.check.proven import (
    ProvenEntry,
    ProvenRange,
    RangeLabel,
    label_against_range,
    load_proven_range,
    version_key,
)


def _range(**components: list[str]) -> ProvenRange:
    return ProvenRange(
        revision="test",
        components={
            name: [ProvenEntry(version=v, where="bed", when="2026-09-24") for v in versions]
            for name, versions in components.items()
        },
    )


class TestVersionKey:
    @pytest.mark.parametrize(
        ("component", "version", "key"),
        [
            ("iproute2", "ss170501", [0, 170501]),
            ("iproute2", "6.1.0", [1, 6, 1, 0]),
            ("iproute2", "5.15", [1, 5, 15]),
            ("kernel", "6.8.0-86-generic", [6, 8, 0]),
            ("kernel", "3.10.0-1160.el7.aarch64", [3, 10, 0]),
            ("socat", "1.7.4.4", [1, 7, 4, 4]),
            ("bash", "5.2.21(1)-release", [5, 2, 21]),
        ],
    )
    def test_keys(self, component: str, version: str, key: list[int]) -> None:
        assert version_key(component, version) == key

    @pytest.mark.parametrize("version", ["", "banana", "ssXYZ"])
    def test_unparseable_is_none(self, version: str) -> None:
        assert version_key("iproute2", version) is None

    def test_date_era_iproute2_sorts_before_every_dotted_one(self) -> None:
        old = version_key("iproute2", "ss991231")
        new = version_key("iproute2", "4.0")
        assert old is not None
        assert new is not None
        assert old < new


class TestLabel:
    def test_within_older_newer(self) -> None:
        proven = _range(iproute2=["ss170501", "6.1.0"])
        assert label_against_range(proven, "iproute2", "5.10.0") is RangeLabel.WITHIN
        assert label_against_range(proven, "iproute2", "ss170501") is RangeLabel.WITHIN
        assert label_against_range(proven, "iproute2", "ss160101") is RangeLabel.OLDER
        assert label_against_range(proven, "iproute2", "6.9.0") is RangeLabel.NEWER

    def test_unordered_component_is_membership(self) -> None:
        proven = _range(isa=["aarch64"])
        assert label_against_range(proven, "isa", "aarch64") is RangeLabel.WITHIN
        assert label_against_range(proven, "isa", "x86_64") is RangeLabel.OUTSIDE

    @pytest.mark.parametrize("version", [None, "banana"])
    def test_missing_or_unparseable_version_is_unknown(self, version: str | None) -> None:
        proven = _range(iproute2=["6.1.0"])
        assert label_against_range(proven, "iproute2", version) is RangeLabel.UNKNOWN

    def test_component_without_entries_is_unknown(self) -> None:
        assert label_against_range(_range(), "kernel", "6.8.0") is RangeLabel.UNKNOWN


def test_packaged_file_loads_and_names_its_seed_versions() -> None:
    proven = load_proven_range()
    assert proven.revision
    versions = {e.version for e in proven.components["iproute2"]}
    assert {"ss170501", "6.1.0"} <= versions
    assert {e.version for e in proven.components["isa"]} == {"aarch64"}
