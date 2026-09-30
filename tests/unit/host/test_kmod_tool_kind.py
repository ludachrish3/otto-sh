"""The ``kmod`` dev-tool kind: a kernel module placed on a host, and its ``kmodcov`` subtype."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from otto.declared import DeclaredEntry
from otto.host import kmod_tool_kind  # noqa: F401 — import registers the kinds
from otto.host.dev_tool import DEV_TOOL_KINDS
from otto.host.kmod_tool_kind import KmodTool
from otto.result import Result
from otto.utils import Status


def _entry(kind="kmod", **params):
    params.setdefault("artifact", "build/tracer.ko")
    return DeclaredEntry(
        name=params.pop("name", "tracer"),
        kind=kind,
        seam="dev_tools",
        owner="r",
        base_dir=Path("/repo"),
        match={},
        params=params,
    )


class _Host(SimpleNamespace):
    def __init__(self, *, loaded=(), **attrs):
        super().__init__(**attrs)
        self.id = attrs.get("id", "test1")
        self.default_dest_dir = attrs.get("default_dest_dir", Path())

        async def _login_home():
            return Path(attrs.get("home", "/home/tester"))

        self.login_home = _login_home
        self.load = AsyncMock(return_value=Result(Status.Success))
        self.unload = AsyncMock(return_value=Result(Status.Success))
        self.lsmod = AsyncMock(return_value=Result(Status.Success, value=list(loaded)))
        self.dev_tools = []
        self.products = []


def _build(host=None, kind="kmod", **params):
    return DEV_TOOL_KINDS.get(kind)(_entry(kind=kind, **params), host or _Host())


def test_kmod_is_registered_for_dev_tools():
    assert "kmod" in DEV_TOOL_KINDS


def test_kmod_tool_defaults_module_name_from_the_artifact_stem():
    tool = _build(artifact="build/my-tracer.ko")
    assert isinstance(tool, KmodTool)
    assert tool.artifact == Path("/repo/build/my-tracer.ko")
    assert tool.module_name == "my_tracer"
    assert tool.params == ""


def test_kmod_tool_reads_module_name_and_params():
    tool = _build(module_name="tracer_x", params="debug=1")
    assert (tool.module_name, tool.params) == ("tracer_x", "debug=1")


@pytest.mark.parametrize(
    ("params", "fragment"),
    [
        ({"artifact": "build/tracer.bin"}, "'artifact' must be a .ko"),
        ({"cov_dir": "/x"}, "unknown param(s): ['cov_dir']"),
        ({"params": "{name}"}, "'params' takes no placeholders"),
        (
            {"bogus": 1},
            (
                "kind 'kmod' got unknown param(s): ['bogus']; valid: artifact, stage_dir, "
                "module_name, params"
            ),
        ),
    ],
)
def test_kmod_tool_rejects_bad_params_naming_the_entry(params, fragment):
    with pytest.raises(ValueError, match=r"\[\[dev_tools\]\] 'tracer'") as ei:
        _build(**params)
    assert fragment in str(ei.value)


def test_kmod_tool_refuses_a_host_without_the_module_verbs():
    host = SimpleNamespace(id="board1", dev_tools=[], products=[])
    with pytest.raises(ValueError, match=r"board1.*no load/unload/lsmod"):
        _build(host)


@pytest.mark.asyncio
async def test_kmod_tool_stage_is_a_noop_and_install_loads_with_params():
    host = _Host()
    tool = _build(host, params="debug=1")
    assert (await tool.stage(host)).is_ok
    assert (await tool.install(host)).is_ok
    host.load.assert_awaited_once_with(
        Path("/repo/build/tracer.ko"), "tracer", params="debug=1", dest_dir=Path("/home/tester")
    )


@pytest.mark.asyncio
async def test_kmod_tool_install_is_a_noop_success_when_the_module_is_resident():
    # `install-tools` run twice, or a run whose cleanup never got to unload
    # the module, must not turn into an `insmod` that fails with `File
    # exists` — the verb is idempotent, like UnixHost.unload.
    host = _Host(loaded=["ext4", "tracer"])
    tool = _build(host)
    assert (await tool.install(host)).is_ok
    host.load.assert_not_awaited()


@pytest.mark.asyncio
async def test_kmod_tool_is_installed_reads_lsmod_and_uninstall_unloads():
    host = _Host(loaded=["ext4", "tracer"])
    tool = _build(host)
    assert await tool.is_installed(host) is True
    assert await _build(host, module_name="other").is_installed(host) is False
    host.lsmod.return_value = Result(Status.Error, value=[], msg="boom")
    assert await tool.is_installed(host) is False
    assert (await tool.uninstall(host)).is_ok
    host.unload.assert_awaited_once_with("tracer")


from otto import kmodcov
from otto.host.kmod_tool_kind import (
    KMODCOV_MODULE_NAME,
    KmodcovTool,
    check_kmodcov_bindings,
    kmodcov_tool_for,
)


def _fake_ko(tmp_path: Path, version: str | None = f"1.6.0+kmodcov{kmodcov.INTERFACE}") -> Path:
    ko = tmp_path / "otto_kmodcov.ko"
    strings = ["srcversion=ABC", "vermagic=6.8.0-86-generic SMP"]
    if version is not None:
        strings.append(f"version={version}")
    ko.write_bytes(b"\x7fELF\x00" + b"\x00".join(s.encode() for s in strings) + b"\x00")
    return ko


def _kmodcov(host=None, tmp_path: Path | None = None, **params):
    if tmp_path is not None:
        params.setdefault("artifact", str(_fake_ko(tmp_path)))
    params.setdefault("artifact", "build/lib/otto_kmodcov.ko")
    params.setdefault("name", "kmodcov-6.8")
    return _build(host, kind="kmodcov", **params)


def test_kmodcov_is_registered_and_fixes_the_module_name():
    assert "kmodcov" in DEV_TOOL_KINDS
    tool = _kmodcov()
    assert isinstance(tool, KmodcovTool)
    assert tool.module_name == KMODCOV_MODULE_NAME == "otto_kmodcov"
    assert tool.source is None


def test_kmodcov_reads_source_anchored_to_the_repo():
    tool = _kmodcov(source="third_party/otto_kmodcov")
    assert tool.source == Path("/repo/third_party/otto_kmodcov")


@pytest.mark.parametrize(
    ("params", "fragment"),
    [
        ({"module_name": "x"}, "kind 'kmodcov' got unknown param(s): ['module_name']"),
        ({"params": "cov_dir=/x"}, "'params' must not set cov_dir"),
        ({"bogus": 1}, "valid: artifact, stage_dir, params, source"),
    ],
)
def test_kmodcov_rejects_bad_params_naming_the_entry(params, fragment):
    with pytest.raises(ValueError, match=r"\[\[dev_tools\]\] 'kmodcov-6.8'") as ei:
        _kmodcov(**params)
    assert fragment in str(ei.value)


def test_kmodcov_refuses_a_built_ko_of_another_interface_at_lab_load(tmp_path: Path):
    ko = _fake_ko(tmp_path, version=f"1.2.0+kmodcov{kmodcov.INTERFACE + 1}")
    with pytest.raises(ValueError, match=r"kmodcov-6\.8") as ei:
        _kmodcov(artifact=str(ko), source="third_party/otto_kmodcov")
    message = str(ei.value)
    assert "kmodcov-6.8" in message and str(ko) in message  # noqa: PT018
    assert (  # noqa: PT018
        f"kmodcov{kmodcov.INTERFACE + 1}" in message and f"kmodcov{kmodcov.INTERFACE}" in message
    )
    assert "otto cov kmodcov export /repo/third_party/otto_kmodcov" in message


def test_kmodcov_refuses_a_built_ko_with_no_version_at_lab_load(tmp_path: Path):
    ko = _fake_ko(tmp_path, version=None)
    with pytest.raises(ValueError, match="carries no MODULE_VERSION"):
        _kmodcov(artifact=str(ko))


def test_kmodcov_refuses_a_version_with_no_interface_number_without_printing_none(tmp_path: Path):
    # A MODULE_VERSION with no `+kmodcov<n>` suffix has no number to print: the
    # message must SAY so rather than render the missing one as "kmodcovNone".
    ko = _fake_ko(tmp_path, version="1.6.0")
    with pytest.raises(ValueError, match=r"kmodcov-6\.8") as ei:
        _kmodcov(artifact=str(ko), source="third_party/otto_kmodcov")
    message = str(ei.value)
    assert "kmodcovNone" not in message
    assert "reports 1.6.0, which carries no kmodcov interface number" in message
    assert f"kmodcov{kmodcov.INTERFACE}" in message
    assert "otto cov kmodcov export /repo/third_party/otto_kmodcov" in message


def test_kmodcov_remedy_without_a_source_never_guesses_the_artifacts_parent(tmp_path: Path):
    # With no `source` declared, the artifact's parent is the BUILD directory:
    # advising an export into it would put sources where the .ko lands.
    ko = _fake_ko(tmp_path, version=f"1.2.0+kmodcov{kmodcov.INTERFACE + 1}")
    with pytest.raises(ValueError, match=r"kmodcov-6\.8") as ei:
        _kmodcov(artifact=str(ko))
    message = str(ei.value)
    assert str(tmp_path) not in message.split(str(ko), 1)[1]
    remedy = (
        "re-export the vendored library (declare `source` on the entry to have it named here) "
        "and rebuild"
    )
    assert message.endswith(remedy), message


def test_kmodcov_accepts_a_matching_ko_and_an_unbuilt_one(tmp_path: Path):
    _kmodcov(tmp_path=tmp_path)  # matching interface
    _kmodcov(artifact=str(tmp_path / "not-built-yet.ko"))  # absent: checked at install instead


@pytest.mark.asyncio
async def test_kmodcov_install_checks_the_interface_before_loading(tmp_path: Path):
    host = _Host()
    tool = _kmodcov(host, artifact=str(tmp_path / "later.ko"))
    result = await tool.install(host)
    assert result.status is Status.Error
    assert "later.ko" in result.msg and "not built" in result.msg  # noqa: PT018
    host.load.assert_not_awaited()
    _fake_ko(tmp_path, version=f"1.0+kmodcov{kmodcov.INTERFACE + 5}").rename(tmp_path / "later.ko")
    result = await tool.install(host)
    assert result.status is Status.Error
    assert f"kmodcov{kmodcov.INTERFACE + 5}" in result.msg
    host.load.assert_not_awaited()
    _fake_ko(tmp_path).rename(tmp_path / "later.ko")
    assert (await tool.install(host)).is_ok
    host.load.assert_awaited_once_with(
        tmp_path / "later.ko", "otto_kmodcov", params="", dest_dir=Path("/home/tester")
    )


@pytest.mark.asyncio
async def test_kmodcov_install_is_a_noop_success_when_the_library_is_resident(tmp_path: Path):
    # The library outlives an aborted run, and `install-tools` may be asked
    # for twice: a resident otto_kmodcov is left in place, never re-insmod'd.
    host = _Host(loaded=["otto_kmodcov"])
    tool = _kmodcov(host, tmp_path=tmp_path)
    assert (await tool.install(host)).is_ok
    host.load.assert_not_awaited()


@pytest.mark.asyncio
async def test_kmodcov_install_checks_the_interface_before_the_residency_shortcut(tmp_path: Path):
    # Residency never excuses a wrong-interface artifact: the check runs
    # first, so the refusal still names both interface numbers.
    host = _Host(loaded=["otto_kmodcov"])
    tool = _kmodcov(host, artifact=str(tmp_path / "later.ko"))
    _fake_ko(tmp_path, version=f"1.0+kmodcov{kmodcov.INTERFACE + 5}").rename(tmp_path / "later.ko")
    result = await tool.install(host)
    assert result.status is Status.Error
    assert f"kmodcov{kmodcov.INTERFACE + 5}" in result.msg
    host.load.assert_not_awaited()


def test_kmodcov_tool_for_finds_the_one_kmodcov_tool_among_dev_tools():
    host = _Host()
    plain = _build(host)
    tool = _kmodcov(host)
    host.dev_tools = [plain, tool]
    assert kmodcov_tool_for(host) is tool
    host.dev_tools = [plain]
    assert kmodcov_tool_for(host) is None


def test_two_kmodcov_tools_on_one_host_are_refused_naming_both():
    host = _Host()
    host.dev_tools = [_kmodcov(host, name="kmodcov-6.8"), _kmodcov(host, name="kmodcov-6.5")]
    with pytest.raises(ValueError, match="test1") as ei:
        check_kmodcov_bindings(host)
    assert "kmodcov-6.8" in str(ei.value) and "kmodcov-6.5" in str(ei.value)  # noqa: PT018


def test_a_module_coverage_product_with_no_kmodcov_tool_is_refused_naming_all_three():
    from otto.host import kmod_kind  # noqa: F401 — import registers the product kind
    from otto.host.product import PRODUCT_KINDS

    host = _Host()
    entry = DeclaredEntry(
        name="demo",
        kind="kmod",
        seam="products",
        owner="r",
        base_dir=Path("/repo"),
        match={},
        params={"artifact": "build/demo.ko", "coverage": "module"},
    )
    host.products = [PRODUCT_KINDS.get("kmod")(entry, host)]
    with pytest.raises(ValueError, match="needs otto_kmodcov") as ei:
        check_kmodcov_bindings(host)
    message = str(ei.value)
    assert "'demo'" in message
    assert "test1" in message
    assert "kind 'kmodcov'" in message
    host.dev_tools = [_kmodcov(host)]
    check_kmodcov_bindings(host)


def test_one_or_no_kmodcov_tool_passes_the_binding_check():
    host = _Host()
    check_kmodcov_bindings(host)
    host.dev_tools = [_kmodcov(host)]
    check_kmodcov_bindings(host)


# ── stage_dir (issue #368) ───────────────────────────────────────────────────


def _tool(kind, host, tmp_path, **params):
    """Build either dev-tool kind with an artifact its own install accepts."""
    if kind == "kmodcov":
        return _kmodcov(host, tmp_path=tmp_path, **params)
    params.setdefault("artifact", str(_fake_ko(tmp_path)))
    return _build(host, kind="kmod", **params)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["kmod", "kmodcov"])
async def test_dev_tool_install_hands_load_the_resolved_stage_dir(kind, tmp_path: Path):
    host = _Host(default_dest_dir=Path("/srv/stage"))
    tool = _tool(kind, host, tmp_path, stage_dir="/opt/mods")
    assert tool.stage_dir == Path("/opt/mods")
    assert (await tool.install(host)).is_ok
    assert host.load.await_args.kwargs["dest_dir"] == Path("/opt/mods")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["kmod", "kmodcov"])
async def test_dev_tool_install_falls_back_to_the_hosts_transfer_default(kind, tmp_path: Path):
    host = _Host(default_dest_dir=Path("/srv/stage"))
    assert (await _tool(kind, host, tmp_path).install(host)).is_ok
    assert host.load.await_args.kwargs["dest_dir"] == Path("/srv/stage")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["kmod", "kmodcov"])
async def test_dev_tool_install_falls_back_to_the_login_home(kind, tmp_path: Path):
    host = _Host()  # the repo-wide case: no default_dest_dir at all
    assert (await _tool(kind, host, tmp_path).install(host)).is_ok
    assert host.load.await_args.kwargs["dest_dir"] == Path("/home/tester")


@pytest.mark.parametrize("kind", ["kmod", "kmodcov"])
def test_dev_tool_kinds_refuse_the_retired_dest_dir_key(kind):
    with pytest.raises(ValueError, match=r"(?s)'dest_dir'.*'stage_dir'"):
        _build(kind=kind, dest_dir="/tmp")


@pytest.mark.parametrize("kind", ["kmod", "kmodcov"])
def test_dev_tool_kinds_refuse_a_relative_stage_dir(kind):
    with pytest.raises(ValueError, match="absolute"):
        _build(kind=kind, stage_dir="mods")
