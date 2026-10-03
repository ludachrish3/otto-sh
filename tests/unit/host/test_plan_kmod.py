"""The kmod product and the kmod/kmodcov dev tools plan what UnixHost.load/unload really do."""

from pathlib import Path

import pytest

from otto.declared import DeclaredEntry
from otto.host import kmod_kind, kmod_tool_kind  # noqa: F401 — imports register the kinds
from otto.host.dev_tool import DEV_TOOL_KINDS
from otto.host.kmod_kind import KmodProduct
from otto.host.kmod_tool_kind import KmodTool
from otto.host.product import LOGIN_HOME_PLACEHOLDER, PRODUCT_KINDS
from otto.kmodcov import INTERFACE

MODULES = "cat /proc/modules"


@pytest.fixture(autouse=True)
def _library_speaks_this_interface(monkeypatch):
    """Every .ko here is a stub; the kmodcov kind reads its MODULE_VERSION at build and install."""
    monkeypatch.setattr(
        "otto.kmodcov.library.modinfo_version", lambda _path: f"1.0+kmodcov{INTERFACE}"
    )


def _kmod(host, tmp_path, artifact="kcov.ko", **params):
    (tmp_path / artifact).write_bytes(b"\x7fELF")
    entry = DeclaredEntry(
        name="kcov",
        kind="kmod",
        seam="products",
        owner="r1",
        base_dir=tmp_path,
        match={},
        params={"artifact": artifact, **params},
    )
    return PRODUCT_KINDS.build([entry], host)[0]


def _tool(host, tmp_path, kind="kmod", name="helper", artifact=None, **params):
    artifact = artifact or f"{name}.ko"
    (tmp_path / artifact).write_bytes(b"\x7fELF")
    entry = DeclaredEntry(
        name=name,
        kind=kind,
        seam="dev_tools",
        owner="r1",
        base_dir=tmp_path,
        match={},
        params={"artifact": artifact, **params},
    )
    return DEV_TOOL_KINDS.build([entry], host)[0]


def _actions(lines):
    return [line for line in lines if line != MODULES]


@pytest.mark.asyncio
async def test_kmod_tool_plan_is_the_load_and_the_unload(plan_recorder, tmp_path):
    host = plan_recorder
    host.default_dest_dir = Path("/opt/mods")
    tool = _tool(host, tmp_path, params="debug=1")
    plan = tool.plan(host)
    assert (await tool.install(host)).is_ok
    assert (
        _actions(host.take())
        == plan.install
        == [
            f"PUT {tmp_path}/helper.ko -> /opt/mods",
            "sudo insmod /opt/mods/helper.ko debug=1",
            "rm -f /opt/mods/helper.ko",
        ]
    )
    host.script(MODULES, "helper 16384 0 - Live 0x0\n")
    assert (await tool.uninstall(host)).is_ok
    assert _actions(host.take()) == plan.uninstall == ["sudo rmmod helper"]
    assert plan.stage == []
    assert plan.unchecked == [
        (
            "whether helper is already resident on h1 (cat /proc/modules): install skips a "
            "resident module, uninstall runs rmmod only for one"
        ),
        "sudo is assumed because the login user is not root; a real run measures how h1 elevates",
    ]


@pytest.mark.asyncio
async def test_kmod_tool_install_skips_a_resident_module_and_the_plan_says_so(
    plan_recorder, tmp_path
):
    host = plan_recorder
    host.default_dest_dir = Path("/opt/mods")
    tool = _tool(host, tmp_path)
    host.script(MODULES, "helper 16384 0 - Live 0x0\n")
    assert (await tool.install(host)).is_ok
    assert _actions(host.take()) == []  # the real run did nothing
    assert (
        tool.plan(host).install[1] == "sudo insmod /opt/mods/helper.ko"
    )  # the plan still shows it
    assert "install skips a resident module" in tool.plan(host).unchecked[0]


def test_kmod_tool_plan_names_the_login_home_gap_like_the_collision_check(plan_recorder, tmp_path):
    host = plan_recorder  # no default_dest_dir, no cached login home
    tool = _tool(host, tmp_path)
    plan = tool.plan(host)
    assert plan.install == [
        f"PUT {tmp_path}/helper.ko -> {LOGIN_HOME_PLACEHOLDER}",
        "sudo insmod '<login home>/helper.ko'",
        "rm -f '<login home>/helper.ko'",
    ]
    assert plan.unchecked[0] == (
        "the login home — dev tool 'helper' declares no stage_dir and h1 no default_dest_dir"
    )
    assert host.home_reads == 0


def test_kmod_product_plan_names_its_own_login_home_gap(plan_recorder, tmp_path):
    host = plan_recorder
    plan = _kmod(host, tmp_path).plan(host)
    assert plan.install[0] == f"PUT {tmp_path}/kcov.ko -> {LOGIN_HOME_PLACEHOLDER}"
    assert plan.unchecked[0] == (
        "the login home — product 'kcov' declares no stage_dir and h1 no default_dest_dir"
    )
    assert host.home_reads == 0


def test_module_coverage_names_both_login_home_gaps(plan_recorder, tmp_path):
    host = plan_recorder
    host.dev_tools = [_tool(host, tmp_path, kind="kmodcov", name="otto_kmodcov")]
    plan = _kmod(host, tmp_path, coverage="module").plan(host)
    gaps = [line for line in plan.unchecked if line.startswith("the login home")]
    assert gaps == [
        "the login home — product 'kcov' declares no stage_dir and h1 no default_dest_dir",
        "the login home — dev tool 'otto_kmodcov' declares no stage_dir and h1 no default_dest_dir",
    ]
    assert host.home_reads == 0


def test_kmodcov_tool_plan_adds_the_interface_check(plan_recorder, tmp_path):
    host = plan_recorder
    host.default_dest_dir = Path("/opt/mods")
    tool = _tool(host, tmp_path, kind="kmodcov", name="otto_kmodcov")
    plan = tool.plan(host)
    assert plan.install[1] == "sudo insmod /opt/mods/otto_kmodcov.ko"
    assert plan.unchecked[-1] == (
        f"the interface check of {tmp_path}/otto_kmodcov.ko (modinfo MODULE_VERSION): a .ko "
        "this otto cannot drive is refused before anything is sent"
    )


@pytest.mark.asyncio
async def test_kmod_product_without_coverage_plans_the_load(plan_recorder, tmp_path):
    host = plan_recorder
    host.default_dest_dir = Path("/opt/mods")
    product = _kmod(host, tmp_path, params="debug=1")
    plan = product.plan(host)
    assert (await product.stage(host)).is_ok
    assert host.take() == [] == plan.stage
    assert (await product.install(host)).is_ok
    assert (
        _actions(host.take())
        == plan.install
        == [
            f"PUT {tmp_path}/kcov.ko -> /opt/mods",
            "sudo insmod /opt/mods/kcov.ko debug=1",
            "rm -f /opt/mods/kcov.ko",
        ]
    )
    host.script(MODULES, "kcov 16384 0 - Live 0x0\n")
    assert (await product.uninstall(host)).is_ok
    assert _actions(host.take()) == plan.uninstall == ["sudo rmmod kcov"]
    assert plan.unchecked == [
        "whether kcov is resident on h1 (cat /proc/modules): uninstall runs rmmod only then",
        "sudo is assumed because the login user is not root; a real run measures how h1 elevates",
    ]


@pytest.mark.asyncio
async def test_kmod_product_with_module_coverage_plans_the_library_first(plan_recorder, tmp_path):
    host = plan_recorder
    host.default_dest_dir = Path("/opt/mods")
    library = _tool(host, tmp_path, kind="kmodcov", name="otto_kmodcov")
    host.dev_tools = [library]
    product = _kmod(host, tmp_path, coverage="module", cov_dir="/tmp/kcov")
    plan = product.plan(host)
    assert (await product.install(host)).is_ok
    assert (
        _actions(host.take())
        == plan.install
        == [
            f"PUT {tmp_path}/otto_kmodcov.ko -> /opt/mods",
            "sudo insmod /opt/mods/otto_kmodcov.ko",
            "rm -f /opt/mods/otto_kmodcov.ko",
            f"PUT {tmp_path}/kcov.ko -> /opt/mods",
            "sudo insmod /opt/mods/kcov.ko cov_dir=/tmp/kcov",
            "rm -f /opt/mods/kcov.ko",
        ]
    )
    assert plan.unchecked == [
        (
            "whether otto_kmodcov is already resident on h1 (cat /proc/modules): the library is "
            "loaded only when it is not"
        ),
        (
            f"the interface check of {tmp_path}/otto_kmodcov.ko (modinfo MODULE_VERSION): a .ko "
            "this otto cannot drive is refused before anything is sent"
        ),
        "whether kcov is resident on h1 (cat /proc/modules): uninstall runs rmmod only then",
        "sudo is assumed because the login user is not root; a real run measures how h1 elevates",
    ]


@pytest.mark.asyncio
async def test_a_resident_library_is_skipped_by_the_real_run_and_named_by_the_plan(
    plan_recorder, tmp_path
):
    host = plan_recorder
    host.default_dest_dir = Path("/opt/mods")
    host.dev_tools = [_tool(host, tmp_path, kind="kmodcov", name="otto_kmodcov")]
    product = _kmod(host, tmp_path, coverage="module")
    host.script(MODULES, "otto_kmodcov 16384 0 - Live 0x0\n")
    assert (await product.install(host)).is_ok
    recorded = _actions(host.take())
    assert recorded == product.plan(host).install[3:]  # only the consumer's own load ran
    assert "the library is loaded only when it is not" in product.plan(host).unchecked[0]


def test_module_coverage_without_a_kmodcov_tool_is_a_refusal_not_a_guess(plan_recorder, tmp_path):
    host = plan_recorder
    host.default_dest_dir = Path("/opt/mods")
    product = _kmod(host, tmp_path, coverage="module")
    plan = product.plan(host)
    assert plan.install == []
    assert plan.unchecked == [
        (
            'coverage = "module" needs otto_kmodcov on host h1, and no [[dev_tools]] entry of '
            "kind 'kmodcov' matches that host: the install is refused"
        )
    ]


@pytest.mark.asyncio
async def test_a_quoted_name_dir_and_params_are_quoted_alike_by_the_hook_and_the_plan(
    plan_recorder, tmp_path
):
    host = plan_recorder
    host.default_dest_dir = Path("/opt/my mods")
    tool = _tool(
        host, tmp_path, name="helper", artifact="k cov.ko", module_name="k_cov", params="debug=1"
    )
    plan = tool.plan(host)
    assert (await tool.install(host)).is_ok
    recorded = _actions(host.take())
    assert recorded == plan.install
    assert recorded == [
        f"PUT {tmp_path}/k cov.ko -> /opt/my mods",
        "sudo insmod '/opt/my mods/k cov.ko' debug=1",
        "rm -f '/opt/my mods/k cov.ko'",
    ]

    library = _tool(host, tmp_path, kind="kmodcov", name="otto_kmodcov")
    host.dev_tools = [library]
    product = _kmod(
        host, tmp_path, artifact="k cov.ko", coverage="module", cov_dir="/tmp/my cov", params="a=1"
    )
    plan = product.plan(host)
    assert (await product.install(host)).is_ok
    recorded = _actions(host.take())
    assert recorded == plan.install
    assert recorded[-2] == "sudo insmod '/opt/my mods/k cov.ko' a=1 'cov_dir=/tmp/my cov'"


@pytest.mark.asyncio
async def test_a_dashed_module_name_is_normalised_alike_by_unload_and_the_plan(
    plan_recorder, tmp_path
):
    host = plan_recorder
    host.default_dest_dir = Path("/opt/mods")
    declared = _tool(host, tmp_path, name="declared", artifact="my-mod.ko", module_name="my-mod")
    derived = _tool(host, tmp_path, name="derived", artifact="other-mod.ko")
    product = _kmod(host, tmp_path, artifact="prod-mod.ko")
    for subject, resident in [
        (declared, "my_mod"),
        (derived, "other_mod"),
        (product, "prod_mod"),
    ]:
        plan = subject.plan(host)
        assert (await subject.install(host)).is_ok
        host.take()
        host.script(MODULES, f"{resident} 16384 0 - Live 0x0\n")
        assert (await subject.uninstall(host)).is_ok
        assert _actions(host.take()) == plan.uninstall == [f"sudo rmmod {resident}"]


@pytest.mark.asyncio
async def test_a_dashed_declared_name_installs_alike_by_the_hook_and_the_plan(
    plan_recorder, tmp_path
):
    host = plan_recorder
    host.default_dest_dir = Path("/opt/mods")
    tool = _tool(host, tmp_path, name="declared", artifact="my-mod.ko", module_name="my-mod")
    plan = tool.plan(host)
    assert (await tool.install(host)).is_ok
    assert _actions(host.take()) == plan.install


@pytest.mark.asyncio
async def test_a_root_stage_dir_joins_without_a_doubled_slash(plan_recorder, tmp_path):
    host = plan_recorder
    tool = _tool(host, tmp_path, stage_dir="/")
    plan = tool.plan(host)
    assert (await tool.install(host)).is_ok
    recorded = _actions(host.take())
    assert recorded == plan.install
    assert recorded[1:] == ["sudo insmod /helper.ko", "rm -f /helper.ko"]


@pytest.mark.asyncio
async def test_a_wrong_interface_is_refused_before_anything_is_sent_and_the_plan_says_so(
    plan_recorder, tmp_path, monkeypatch
):
    host = plan_recorder
    host.default_dest_dir = Path("/opt/mods")
    library = _tool(host, tmp_path, kind="kmodcov", name="otto_kmodcov")
    host.dev_tools = [library]
    product = _kmod(host, tmp_path, coverage="module")
    monkeypatch.setattr("otto.kmodcov.library.modinfo_version", lambda _path: "1.0+kmodcov0")

    assert not (await library.install(host)).is_ok
    assert _actions(host.take()) == []
    assert not (await product.install(host)).is_ok
    assert _actions(host.take()) == []  # nothing but the /proc/modules read reached the host
    check = f"the interface check of {tmp_path}/otto_kmodcov.ko (modinfo MODULE_VERSION)"
    assert any(line.startswith(check) for line in library.plan(host).unchecked)
    assert any(line.startswith(check) for line in product.plan(host).unchecked)


@pytest.mark.asyncio
async def test_a_root_login_plans_the_bare_insmod_and_rmmod_the_tool_really_runs(
    root_plan_recorder, tmp_path
):
    host = root_plan_recorder
    host.default_dest_dir = Path("/opt/mods")
    tool = _tool(host, tmp_path, params="debug=1")
    plan = tool.plan(host)
    assert (await tool.install(host)).is_ok
    assert (
        _actions(host.take())
        == plan.install
        == [
            f"PUT {tmp_path}/helper.ko -> /opt/mods",
            "insmod /opt/mods/helper.ko debug=1",
            "rm -f /opt/mods/helper.ko",
        ]
    )
    host.script(MODULES, "helper 16384 0 - Live 0x0\n")
    assert (await tool.uninstall(host)).is_ok
    assert _actions(host.take()) == plan.uninstall == ["rmmod helper"]
    assert not any("sudo" in line for line in plan.unchecked)


@pytest.mark.asyncio
async def test_a_root_login_plans_the_bare_insmod_and_rmmod_the_kmodcov_tool_really_runs(
    root_plan_recorder, tmp_path
):
    host = root_plan_recorder
    host.default_dest_dir = Path("/opt/mods")
    tool = _tool(host, tmp_path, kind="kmodcov", name="otto_kmodcov")
    plan = tool.plan(host)
    assert (await tool.install(host)).is_ok
    assert (
        _actions(host.take())
        == plan.install
        == [
            f"PUT {tmp_path}/otto_kmodcov.ko -> /opt/mods",
            "insmod /opt/mods/otto_kmodcov.ko",
            "rm -f /opt/mods/otto_kmodcov.ko",
        ]
    )
    host.script(MODULES, "otto_kmodcov 16384 0 - Live 0x0\n")
    assert (await tool.uninstall(host)).is_ok
    assert _actions(host.take()) == plan.uninstall == ["rmmod otto_kmodcov"]
    assert not any("sudo" in line for line in plan.unchecked)


@pytest.mark.asyncio
@pytest.mark.parametrize("coverage", ["none", "module"])
async def test_a_root_login_plans_the_bare_insmod_and_rmmod_the_kmod_product_really_runs(
    root_plan_recorder, tmp_path, coverage
):
    host = root_plan_recorder
    host.default_dest_dir = Path("/opt/mods")
    expected = [
        f"PUT {tmp_path}/kcov.ko -> /opt/mods",
        "insmod /opt/mods/kcov.ko",
        "rm -f /opt/mods/kcov.ko",
    ]
    extra = {}
    if coverage == "module":
        host.dev_tools = [_tool(host, tmp_path, kind="kmodcov", name="otto_kmodcov")]
        extra = {"coverage": "module", "cov_dir": "/tmp/kcov"}
        expected = [
            f"PUT {tmp_path}/otto_kmodcov.ko -> /opt/mods",
            "insmod /opt/mods/otto_kmodcov.ko",
            "rm -f /opt/mods/otto_kmodcov.ko",
            f"PUT {tmp_path}/kcov.ko -> /opt/mods",
            "insmod /opt/mods/kcov.ko cov_dir=/tmp/kcov",
            "rm -f /opt/mods/kcov.ko",
        ]
    product = _kmod(host, tmp_path, **extra)
    plan = product.plan(host)
    assert (await product.install(host)).is_ok
    assert _actions(host.take()) == plan.install == expected
    host.script(MODULES, "kcov 16384 0 - Live 0x0\n")
    assert (await product.uninstall(host)).is_ok
    assert _actions(host.take()) == plan.uninstall == ["rmmod kcov"]
    assert not any("sudo" in line for line in plan.unchecked)


def test_a_host_that_names_no_login_is_planned_as_not_root(tmp_path):
    """A base-typed double with no session manager has no configured login to read."""

    class _Bare:
        id = "h1"
        default_dest_dir = Path("/opt/mods")

    plan = KmodTool(name="helper", artifact=tmp_path / "helper.ko").plan(_Bare())
    assert plan.uninstall == ["sudo rmmod helper"]


def test_a_host_with_no_login_home_refuses_the_load_instead_of_planning_one(tmp_path):
    class _NoHome:
        id = "board1"

    host = _NoHome()
    refusal = [
        (
            "product 'kcov' declares no stage_dir and board1 no default_dest_dir, and board1 "
            "has no login home to fall back on: the install is refused"
        )
    ]
    for plan in (
        KmodProduct(name="kcov", artifact=tmp_path / "kcov.ko").plan(host),
        KmodProduct(name="kcov", artifact=tmp_path / "kcov.ko", coverage="module").plan(host),
    ):
        assert plan.stage == plan.install == plan.uninstall == []
        assert plan.unchecked == refusal
    tool = KmodTool(name="helper", artifact=tmp_path / "helper.ko").plan(host)
    assert tool.install == tool.uninstall == []
    assert tool.unchecked == [refusal[0].replace("product 'kcov'", "dev tool 'helper'")]
