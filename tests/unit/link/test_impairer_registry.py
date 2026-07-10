"""IMPAIRERS registry mirrors the transfer-backend registry (spec §5).

No manual snapshot/cleanup needed: the autouse ``_isolate_registries`` fixture
(tests/unit/conftest.py) discovers every module-level ``Registry`` dynamically
and restores it after each test.
"""

from typing import ClassVar

import pytest

from otto.link.impairer import IMPAIRERS, LinkImpairer, build_impairer, register_impairer


class _FakeImpairer(LinkImpairer):
    host_families: ClassVar[frozenset[str]] = frozenset({"unix"})


class TestRegistry:
    def test_register_and_build_roundtrip(self) -> None:
        register_impairer("fake", _FakeImpairer)
        assert build_impairer("fake") is _FakeImpairer
        assert "fake" in IMPAIRERS

    def test_empty_host_families_rejected(self) -> None:
        class _Homeless(LinkImpairer):
            host_families: ClassVar[frozenset[str]] = frozenset()

        with pytest.raises(ValueError, match="host_families is empty"):
            register_impairer("homeless", _Homeless)

    def test_unknown_name_error_lists_hint(self) -> None:
        with pytest.raises(ValueError, match="register_impairer"):
            build_impairer("no-such-impairer")

    def test_origin_recorded_as_this_module(self) -> None:
        register_impairer("fake", _FakeImpairer)
        assert IMPAIRERS.origin("fake").endswith("test_impairer_registry")

    def test_base_methods_are_abstract(self) -> None:
        base = LinkImpairer()
        for call in (
            lambda: base.apply_command("eth0", None),  # type: ignore[arg-type]
            lambda: base.read_command("eth0"),
            lambda: base.clear_command("eth0"),
            lambda: base.parse_read(""),
        ):
            with pytest.raises(NotImplementedError):
                call()
