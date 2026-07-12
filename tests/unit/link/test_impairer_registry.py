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


class TestScopedContract:
    def test_base_impairer_does_not_support_selectors(self) -> None:
        from otto.link.impairer import LinkImpairer

        assert LinkImpairer.supports_selectors is False

    def test_scoped_methods_default_to_not_implemented(self) -> None:
        import pytest

        from otto.link.impairer import LinkImpairer
        from otto.link.params import ImpairmentParams, Selector

        imp = LinkImpairer()
        sel = Selector(5201, "tcp")
        with pytest.raises(NotImplementedError):
            imp.scoped_root_command("eth1")
        with pytest.raises(NotImplementedError):
            imp.scoped_band_command("eth1", 4, ImpairmentParams(delay_ms=1.0))
        with pytest.raises(NotImplementedError):
            imp.scoped_filter_commands("eth1", 4, sel)
        with pytest.raises(NotImplementedError):
            imp.scoped_clear_selector_commands("eth1", 4, sel)
        with pytest.raises(NotImplementedError):
            imp.scoped_read_commands("eth1")
        with pytest.raises(NotImplementedError):
            imp.parse_scoped("", "")

    def test_scoped_state_constructors(self) -> None:
        from otto.link.impairer import FIRST_SELECTOR_BAND, MAX_SELECTORS, ScopedState
        from otto.link.params import ImpairmentParams, Selector

        assert FIRST_SELECTOR_BAND == 4
        assert MAX_SELECTORS == 8
        assert ScopedState.clean().kind == "clean"
        params = ImpairmentParams(delay_ms=50.0)
        whole = ScopedState.whole_link(params)
        assert whole.kind == "whole"
        assert whole.whole == params
        mapping = {Selector(5201, "tcp"): (4, params)}
        scoped = ScopedState.from_selectors(mapping)
        assert scoped.kind == "scoped"
        assert scoped.selectors == mapping
        assert ScopedState.foreign().kind == "foreign"
