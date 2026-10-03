"""DeclaredProduct: the public base with the shell kind's defaults, and a plan that never
describes a hook the subclass replaced."""

from dataclasses import dataclass
from pathlib import Path

import pytest

from otto.declared import DeclaredEntry
from otto.host import DeclaredProduct as LazyDeclaredProduct
from otto.host.declared_product import DeclaredProduct
from otto.host.dev_tool import DevTool
from otto.host.product import PRODUCT_KINDS, Product, ShellProduct
from otto.host.shell_kind import build_declared
from otto.result import Result
from otto.utils import Status


class _Host:
    """Enough host for a plan: a default dest dir, no login home needed."""

    id = "h1"
    default_dest_dir = Path("/opt/stage")
    cached_login_home = None

    async def run(self, cmds, **kwargs):
        return Result(Status.Success)


def _entry(**params):
    params.setdefault("artifact", "build/fw.bin")
    return DeclaredEntry(
        name="fw", kind="shell", seam="products", owner="r1", base_dir=Path("/sut"), params=params
    )


def test_the_lazy_export_is_the_class():
    assert LazyDeclaredProduct is DeclaredProduct


def test_declared_product_serves_both_seams():
    assert issubclass(DeclaredProduct, ShellProduct)
    assert issubclass(DeclaredProduct, Product)
    assert issubclass(DeclaredProduct, DevTool)


def test_the_shell_kind_builds_a_declared_product():
    built = PRODUCT_KINDS.build([_entry(install="make install")], _Host())[0]
    assert type(built) is DeclaredProduct
    assert built.install_cmd == "make install"


def test_the_default_plan_shows_the_declared_strings():
    product = PRODUCT_KINDS.build(
        [_entry(install="make install", uninstall="make clean")], _Host()
    )[0]
    plan = product.plan(_Host())
    assert plan.install == ["make install"]
    assert plan.uninstall == ["make clean"]
    assert plan.unchecked == []


class Overrides(DeclaredProduct):
    async def install(self, host):
        return await host.run("fwload")


class _BaseOverridesInstall(DeclaredProduct):
    async def install(self, host):
        return await host.run("fwload")


class _LeafInheritsInstall(_BaseOverridesInstall):
    pass


def test_the_unchecked_line_names_the_class_that_defines_the_hook():
    product = _LeafInheritsInstall(artifact=Path("/sut/build/fw.bin"), name="fw")
    plan = product.plan(_Host())
    assert plan.unchecked == ["install: _BaseOverridesInstall.install (code; no plan)"]


def test_an_overridden_install_is_unchecked_never_the_declared_string():
    product = Overrides(artifact=Path("/sut/build/fw.bin"), name="fw", install_cmd="make install")
    plan = product.plan(_Host())
    assert plan.install == []
    assert plan.unchecked == ["install: Overrides.install (code; no plan)"]
    assert "make install" not in str(plan)


class OverridesAll(DeclaredProduct):
    async def stage(self, host):
        return Result(Status.Success)

    async def install(self, host):
        return Result(Status.Success)

    async def uninstall(self, host):
        return Result(Status.Success)


def test_every_overridden_hook_gets_its_own_unchecked_line_in_phase_order():
    product = OverridesAll(artifact=Path("/sut/build/fw.bin"), name="fw", uninstall_cmd="rm -f x")
    plan = product.plan(_Host())
    assert (plan.stage, plan.install, plan.uninstall) == ([], [], [])
    assert plan.unchecked == [
        "stage: OverridesAll.stage (code; no plan)",
        "install: OverridesAll.install (code; no plan)",
        "uninstall: OverridesAll.uninstall (code; no plan)",
    ]


class PlansItself(DeclaredProduct):
    async def install(self, host):
        return await host.run("fwload")

    def plan(self, host):
        from otto.host.product import ProductPlan

        return ProductPlan(stage=[], install=["fwload"], uninstall=[], unchecked=[])


def test_a_subclass_that_plans_itself_is_shown_as_it_plans():
    product = PlansItself(artifact=Path("/sut/build/fw.bin"), name="fw")
    plan = product.plan(_Host())
    assert plan.install == ["fwload"]
    assert plan.unchecked == []


def test_a_subclass_with_the_defaults_untouched_plans_like_the_base():
    @dataclass
    class AddsAField(DeclaredProduct):
        slot: int = 0

    product = AddsAField(artifact=Path("/sut/build/fw.bin"), name="fw", install_cmd="go", slot=3)
    assert product.plan(_Host()).install == ["go"]


def test_declared_shell_is_gone():
    from otto.host import shell_kind

    assert not hasattr(shell_kind, "DeclaredShell")
    with pytest.raises(ImportError):
        from otto.host.shell_kind import DeclaredShell  # noqa: F401


class _LoginHomeHost(_Host):
    """No default dir and no cached home, but a login home to discover: the plan's gap line."""

    default_dest_dir = None
    login_home = "/home/lab"


class _NoHomeHost(_Host):
    """No default dir, no cached home, no login home at all: staging by default is refused."""

    default_dest_dir = None
    login_home = None


def test_a_login_home_gap_stays_first_before_an_overridden_install_line():
    product = Overrides(artifact=Path("/sut/build/fw.bin"), name="fw", install_cmd="make install")
    plan = product.plan(_LoginHomeHost())
    assert "login home" in plan.unchecked[0]
    assert plan.unchecked[1] == "install: Overrides.install (code; no plan)"
    assert len(plan.unchecked) == 2


class OverridesStage(DeclaredProduct):
    async def stage(self, host):
        return Result(Status.Success)


@pytest.mark.parametrize("host", [_LoginHomeHost(), _NoHomeHost()], ids=["gap", "refusal"])
def test_an_overridden_stage_skips_the_stage_dir_story(host):
    product = OverridesStage(artifact=Path("/sut/build/fw.bin"), name="fw")
    plan = product.plan(host)
    assert plan.stage == []
    assert plan.unchecked == ["stage: OverridesStage.stage (code; no plan)"]


def test_the_default_stage_still_refuses_on_a_host_with_no_home():
    plan = DeclaredProduct(artifact=Path("/sut/build/fw.bin"), name="fw").plan(_NoHomeHost())
    assert len(plan.unchecked) == 1
    assert "refused" in plan.unchecked[0]


def test_build_declared_builds_the_subclass_it_is_given():
    class Sub(DeclaredProduct):
        pass

    built = build_declared(_entry(install="go"), _Host(), Sub)
    assert type(built) is Sub
    assert built.install_cmd == "go"
