"""IMPAIRERS registry mirrors the transfer-backend registry (spec §5).

No manual snapshot/cleanup needed: the autouse ``_isolate_registries`` fixture
(tests/unit/conftest.py) discovers every module-level ``Registry`` dynamically
and restores it after each test.
"""

from typing import ClassVar

import pytest

from otto.link.impairer import IMPAIRERS, build_impairer, register_impairer
from otto.link.params import LinkImpairer


class _FakeImpairer(LinkImpairer):
    host_families: ClassVar[frozenset[str]] = frozenset({"unix"})


class TestRegistry:
    def test_register_and_build_roundtrip(self) -> None:
        register_impairer("fake", _FakeImpairer)
        assert build_impairer(name="fake") is _FakeImpairer
        assert "fake" in IMPAIRERS

    def test_empty_host_families_rejected(self) -> None:
        class _Homeless(LinkImpairer):
            host_families: ClassVar[frozenset[str]] = frozenset()

        with pytest.raises(ValueError, match="host_families is empty"):
            register_impairer("homeless", _Homeless)

    def test_unknown_name_error_lists_hint(self) -> None:
        with pytest.raises(ValueError, match="register_impairer"):
            build_impairer(name="no-such-impairer")

    def test_origin_recorded_as_this_module(self) -> None:
        register_impairer("fake", _FakeImpairer)
        assert IMPAIRERS.origin(name="fake").endswith("test_impairer_registry")

    def test_base_methods_are_abstract(self) -> None:
        base = LinkImpairer()
        for call in (
            lambda: base.apply_command("eth0", params=None),  # type: ignore[arg-type]
            lambda: base.read_command("eth0"),
            lambda: base.clear_command("eth0"),
            lambda: base.parse_read(""),
        ):
            with pytest.raises(NotImplementedError):
                call()


class TestScopedContract:
    def test_base_impairer_does_not_support_selectors(self) -> None:
        from otto.link.params import LinkImpairer

        assert LinkImpairer.supports_selectors is False

    def test_scoped_methods_default_to_not_implemented(self) -> None:
        import pytest

        from otto.link.params import ImpairmentParams, LinkImpairer, Selector

        imp = LinkImpairer()
        sel = Selector(port=5201, proto="tcp")
        with pytest.raises(NotImplementedError):
            imp.scoped_root_command(netdev="eth1")
        with pytest.raises(NotImplementedError):
            imp.scoped_band_command(netdev="eth1", band=4, params=ImpairmentParams(delay_ms=1.0))
        with pytest.raises(NotImplementedError):
            imp.scoped_filter_commands(netdev="eth1", band=4, selector=sel)
        with pytest.raises(NotImplementedError):
            imp.scoped_clear_selector_commands(netdev="eth1", band=4, selector=sel)
        with pytest.raises(NotImplementedError):
            imp.scoped_read_commands(netdev="eth1")
        with pytest.raises(NotImplementedError):
            imp.parse_scoped(qdisc_output="", filter_output="")

    def test_scoped_state_constructors(self) -> None:
        from otto.link.params import (
            FIRST_SELECTOR_BAND,
            MAX_SELECTORS,
            ImpairmentParams,
            ScopedState,
            Selector,
        )

        assert FIRST_SELECTOR_BAND == 4
        assert MAX_SELECTORS == 8
        assert ScopedState.clean().kind == "clean"
        params = ImpairmentParams(delay_ms=50.0)
        whole = ScopedState.whole_link(params=params)
        assert whole.kind == "whole"
        assert whole.whole == params
        mapping = {Selector(port=5201, proto="tcp"): (4, params)}
        scoped = ScopedState.from_selectors(selectors=mapping)
        assert scoped.kind == "scoped"
        assert scoped.selectors == mapping
        assert ScopedState.foreign().kind == "foreign"
