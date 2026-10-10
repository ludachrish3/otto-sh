"""Conformance cases for the class-valued seams, login proxies, session setups and kinds."""

import functools
import sys

import pytest

from otto.registry import ClassEntry, Ref
from tests.unit.registry import conformance

COVERS = [
    "otto.host.binary_loader:LOADER_CLASSES",
    "otto.host.command_frame:FRAME_CLASSES",
    "otto.host.embedded_filesystem:FILESYSTEM_CLASSES",
    "otto.link.impairer:IMPAIRERS",
    "otto.tunnel.carrier:CARRIERS",
    "otto.host.login_proxy:LOGIN_PROXIES",
    "otto.host.session_setup:SESSION_SETUPS",
    "otto.host.product:PRODUCT_KINDS",
    "otto.host.dev_tool:DEV_TOOL_KINDS",
]

_SCRATCH = "case_seams_mod"

_SCRATCH_SOURCE = (
    """\
from otto.host.binary_loader import BinaryLoader
from otto.host.command_frame import BashFrame
from otto.host.embedded_filesystem import EmbeddedFileSystem
from otto.link.impairer import LinkImpairer
from otto.tunnel.carrier import TunnelCarrier


class CaseFrame(BashFrame):
    type_name = "case-frame"


class WrongFrame(BashFrame):
    type_name = "other"


class CaseLoader(BinaryLoader):
    type_name = "case-loader"


class WrongLoader(BinaryLoader):
    type_name = "other"


class CaseFs(EmbeddedFileSystem):
    type_name = "case-fs"


class WrongFs(EmbeddedFileSystem):
    type_name = "other"


class CaseImpairer(LinkImpairer):
    host_families = frozenset({"unix"})


class WrongImpairer(LinkImpairer):
    host_families = frozenset()


class CaseCarrier(TunnelCarrier):
    supported_protocols = frozenset({"tcp"})


class WrongCarrier(TunnelCarrier):
    supported_protocols = frozenset()


async def case_setup(session, ctx):
    return None


def case_kind(entry, host):
    return None


NOT_CALLABLE = 42


class NotASeamClass:
    type_name = "case-frame"
"""
    ""
)


@pytest.fixture
def scratch(tmp_path, monkeypatch) -> str:
    """An importable module NOT yet imported, so a lazy case can prove nothing imports it early.

    Every ``lazy``/``wrongly_typed_lazy`` ``Ref`` targets it: an otto
    built-in is already imported (and, for frames, has another ``type_name``).
    """
    (tmp_path / f"{_SCRATCH}.py").write_text(_SCRATCH_SOURCE)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, _SCRATCH, raising=False)
    return _SCRATCH


def _subclass(base: type, i: int, **attrs: object) -> type:
    """A distinct subclass of *base* per *i*, so each record compares unequal."""
    return type(f"Case{base.__name__}{i}", (base,), dict(attrs))


def _hook(i: int):
    """A distinct async hook per *i*."""

    async def hook(session, ctx) -> None:
        return None

    hook.__name__ = f"case_hook_{i}"
    return hook


def _factory(i: int):
    """A distinct kind factory per *i*."""

    def factory(entry, host) -> None:
        return None

    factory.__name__ = f"case_factory_{i}"
    return factory


# --- the class seams ----------------------------------------------------------


def test_frame_classes_raw_case(scratch):
    from otto.host.command_frame import FRAME_CLASSES, BashFrame

    conformance.assert_raw_registry(
        FRAME_CLASSES,
        make=lambda i: ("case-frame", ClassEntry(_subclass(BashFrame, i, type_name="case-frame"))),
        invalid=("case-frame", ClassEntry(_subclass(BashFrame, 0, type_name="other"))),
        lazy=("case-frame", ClassEntry(Ref(f"{scratch}:CaseFrame"))),
        wrongly_typed_lazy=("case-frame", ClassEntry(Ref(f"{scratch}:WrongFrame"))),
    )


def test_loader_classes_raw_case(scratch):
    from otto.host.binary_loader import LOADER_CLASSES, BinaryLoader

    conformance.assert_raw_registry(
        LOADER_CLASSES,
        make=lambda i: (
            "case-loader",
            ClassEntry(_subclass(BinaryLoader, i, type_name="case-loader")),
        ),
        invalid=("case-loader", ClassEntry(_subclass(BinaryLoader, 0, type_name="other"))),
        lazy=("case-loader", ClassEntry(Ref(f"{scratch}:CaseLoader"))),
        wrongly_typed_lazy=("case-loader", ClassEntry(Ref(f"{scratch}:WrongLoader"))),
    )


def test_filesystem_classes_raw_case(scratch):
    from otto.host.embedded_filesystem import FILESYSTEM_CLASSES, EmbeddedFileSystem

    conformance.assert_raw_registry(
        FILESYSTEM_CLASSES,
        make=lambda i: (
            "case-fs",
            ClassEntry(_subclass(EmbeddedFileSystem, i, type_name="case-fs")),
        ),
        invalid=("case-fs", ClassEntry(_subclass(EmbeddedFileSystem, 0, type_name="other"))),
        lazy=("case-fs", ClassEntry(Ref(f"{scratch}:CaseFs"))),
        wrongly_typed_lazy=("case-fs", ClassEntry(Ref(f"{scratch}:WrongFs"))),
    )


def test_impairers_raw_case(scratch):
    from otto.link.impairer import IMPAIRERS, LinkImpairer

    conformance.assert_raw_registry(
        IMPAIRERS,
        make=lambda i: (
            "case-impairer",
            ClassEntry(_subclass(LinkImpairer, i, host_families=frozenset({"unix"}))),
        ),
        invalid=(
            "case-impairer",
            ClassEntry(_subclass(LinkImpairer, 0, host_families=frozenset())),
        ),
        lazy=("case-impairer", ClassEntry(Ref(f"{scratch}:CaseImpairer"))),
        wrongly_typed_lazy=("case-impairer", ClassEntry(Ref(f"{scratch}:WrongImpairer"))),
    )


def test_carriers_raw_case(scratch):
    from otto.tunnel.carrier import CARRIERS, TunnelCarrier

    conformance.assert_raw_registry(
        CARRIERS,
        make=lambda i: (
            "case-carrier",
            ClassEntry(_subclass(TunnelCarrier, i, supported_protocols=frozenset({"tcp"}))),
        ),
        invalid=(
            "case-carrier",
            ClassEntry(_subclass(TunnelCarrier, 0, supported_protocols=frozenset())),
        ),
        lazy=("case-carrier", ClassEntry(Ref(f"{scratch}:CaseCarrier"))),
        wrongly_typed_lazy=("case-carrier", ClassEntry(Ref(f"{scratch}:WrongCarrier"))),
    )


# One class object per seam, so the wrapper's record and the raw one compare equal.
def _shared_class(seam: str) -> type:
    from otto.host.binary_loader import BinaryLoader
    from otto.host.command_frame import BashFrame
    from otto.host.embedded_filesystem import EmbeddedFileSystem
    from otto.link.impairer import LinkImpairer
    from otto.tunnel.carrier import TunnelCarrier

    return {
        "frame": lambda: _subclass(BashFrame, 1, type_name="case-frame"),
        "loader": lambda: _subclass(BinaryLoader, 1, type_name="case-loader"),
        "fs": lambda: _subclass(EmbeddedFileSystem, 1, type_name="case-fs"),
        "impairer": lambda: _subclass(LinkImpairer, 1, host_families=frozenset({"unix"})),
        "carrier": lambda: _subclass(TunnelCarrier, 1, supported_protocols=frozenset({"tcp"})),
    }[seam]()


def _class_seam(seam: str):
    """``(table, wrapper, case name)`` for one class seam."""
    from otto.host.binary_loader import LOADER_CLASSES, register_binary_loader
    from otto.host.command_frame import FRAME_CLASSES, register_command_frame
    from otto.host.embedded_filesystem import FILESYSTEM_CLASSES, register_filesystem
    from otto.link.impairer import IMPAIRERS, register_impairer
    from otto.tunnel.carrier import CARRIERS, register_carrier

    return {
        "frame": (FRAME_CLASSES, register_command_frame, "case-frame"),
        "loader": (LOADER_CLASSES, register_binary_loader, "case-loader"),
        "fs": (FILESYSTEM_CLASSES, register_filesystem, "case-fs"),
        "impairer": (IMPAIRERS, register_impairer, "case-impairer"),
        "carrier": (CARRIERS, register_carrier, "case-carrier"),
    }[seam]


_CLASS_SEAMS = ["frame", "loader", "fs", "impairer", "carrier"]


@pytest.mark.parametrize("seam", _CLASS_SEAMS)
def test_class_seam_wrapper_matches_the_raw_path(seam):
    table, wrapper, name = _class_seam(seam)
    cls = _shared_class(seam)
    conformance.assert_wrapper_matches_raw(
        table,
        via_wrapper=functools.partial(wrapper, name, cls),
        record_for=lambda: ClassEntry(cls),
    )


@pytest.mark.parametrize("seam", _CLASS_SEAMS)
def test_a_class_seam_refuses_an_object_of_the_wrong_type(scratch, seam):
    """The wrapper refuses a non-class eagerly; a Ref to a non-subclass fails at get.

    Both are TypeErrors, raised by the seam's one check from its two call sites.
    """
    table, wrapper, name = _class_seam(seam)
    with pytest.raises(TypeError, match="subclass"):
        wrapper(name, object())
    assert name not in table
    wrapper(name, Ref(f"{scratch}:NotASeamClass"))
    for _ in range(2):  # nothing is cached, so the retry re-checks
        with pytest.raises(TypeError, match="subclass"):
            table.get(name)


# --- login proxies -------------------------------------------------------------


def test_login_proxies_raw_and_wrapper_cases():
    from otto.host.login_proxy import LOGIN_PROXIES, LoginProxy, register_login_proxy

    # No eager check (any LoginProxy is valid) and no Ref form (it holds callables).
    conformance.assert_raw_registry(
        LOGIN_PROXIES, make=lambda i: ("case-proxy", LoginProxy(_hook(i)))
    )
    shared = _hook(1)
    conformance.assert_wrapper_matches_raw(
        LOGIN_PROXIES,
        via_wrapper=functools.partial(register_login_proxy, "case-proxy", shared),
        record_for=lambda: LoginProxy(shared),
    )


# --- session setups ------------------------------------------------------------


def test_session_setups_raw_and_wrapper_cases(scratch):
    from otto.host.session_setup import (
        SESSION_SETUPS,
        SessionSetupEntry,
        register_session_setup,
    )

    conformance.assert_raw_registry(
        SESSION_SETUPS,
        make=lambda i: ("case-setup", SessionSetupEntry(_hook(i))),
        invalid=("case-setup", SessionSetupEntry(fn=42)),
        lazy=("case-setup", SessionSetupEntry(Ref(f"{scratch}:case_setup"))),
        wrongly_typed_lazy=("case-setup", SessionSetupEntry(Ref(f"{scratch}:NOT_CALLABLE"))),
    )
    shared = _hook(1)
    conformance.assert_wrapper_matches_raw(
        SESSION_SETUPS,
        via_wrapper=functools.partial(register_session_setup, "case-setup", shared),
        record_for=lambda: SessionSetupEntry(shared),
    )


def test_a_raw_session_setup_with_a_non_callable_fn_is_refused_at_registration():
    """The eager check runs on the raw path too, not only through the wrapper.

    Mutation: call the check only from ``check_resolved``.
    """
    from otto.host.session_setup import SESSION_SETUPS, SessionSetupEntry

    revision = SESSION_SETUPS.revision
    with pytest.raises(TypeError, match="not callable"):
        SESSION_SETUPS.register("x", SessionSetupEntry(fn=42))
    assert "x" not in SESSION_SETUPS
    assert SESSION_SETUPS.revision == revision


# --- kinds ---------------------------------------------------------------------


@pytest.mark.parametrize("seam", ["product", "dev_tool"])
def test_kinds_raw_and_wrapper_cases(scratch, seam):
    from otto.declared import KindEntry
    from otto.host.dev_tool import DEV_TOOL_KINDS, register_dev_tool_kind
    from otto.host.product import PRODUCT_KINDS, register_product_kind

    table, wrapper = {
        "product": (PRODUCT_KINDS, register_product_kind),
        "dev_tool": (DEV_TOOL_KINDS, register_dev_tool_kind),
    }[seam]
    conformance.assert_raw_registry(
        table,
        make=lambda i: ("case-kind", KindEntry(_factory(i))),
        invalid=("case-kind", KindEntry(factory=42)),
        lazy=("case-kind", KindEntry(Ref(f"{scratch}:case_kind"))),
        wrongly_typed_lazy=("case-kind", KindEntry(Ref(f"{scratch}:NOT_CALLABLE"))),
    )
    shared = _factory(1)
    conformance.assert_wrapper_matches_raw(
        table,
        via_wrapper=functools.partial(wrapper, "case-kind", shared),
        record_for=lambda: KindEntry(shared),
    )


@pytest.mark.parametrize("seam", ["product", "dev_tool"])
def test_a_raw_kind_with_a_non_callable_factory_is_refused_at_registration(seam):
    """The eager check runs on the raw path too, not only through the wrapper.

    Mutation: call the check only from ``check_resolved``.
    """
    from otto.declared import KindEntry
    from otto.host.dev_tool import DEV_TOOL_KINDS
    from otto.host.product import PRODUCT_KINDS

    table = {"product": PRODUCT_KINDS, "dev_tool": DEV_TOOL_KINDS}[seam]
    revision = table.revision
    with pytest.raises(TypeError, match="not callable"):
        table.register("x", KindEntry(factory=42))
    assert "x" not in table
    assert table.revision == revision


@pytest.mark.parametrize("seam", ["session_setup", "product_kind", "dev_tool_kind"])
def test_a_callable_seam_refuses_an_object_of_the_wrong_type(scratch, seam):
    """The wrapper refuses a non-callable eagerly; a Ref to one fails at get."""
    from otto.host.dev_tool import DEV_TOOL_KINDS, register_dev_tool_kind
    from otto.host.product import PRODUCT_KINDS, register_product_kind
    from otto.host.session_setup import SESSION_SETUPS, register_session_setup

    table, wrapper = {
        "session_setup": (SESSION_SETUPS, register_session_setup),
        "product_kind": (PRODUCT_KINDS, register_product_kind),
        "dev_tool_kind": (DEV_TOOL_KINDS, register_dev_tool_kind),
    }[seam]
    with pytest.raises(TypeError, match="not callable"):
        wrapper("case-wrong", 42)
    assert "case-wrong" not in table
    wrapper("case-wrong", Ref(f"{scratch}:NOT_CALLABLE"))
    for _ in range(2):
        with pytest.raises(TypeError, match="not callable"):
            table.get("case-wrong")
