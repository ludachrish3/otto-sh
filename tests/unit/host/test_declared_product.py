"""DeclaredProduct: the public base with the shell kind's defaults, and a plan that never
describes a hook the subclass replaced."""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from otto.declared import DeclaredEntry
from otto.host import DeclaredProduct as LazyDeclaredProduct
from otto.host.declared_product import DeclaredProduct
from otto.host.dev_tool import DevTool
from otto.host.docker_image_kind import _docker_image_kind
from otto.host.embedded_kind import _embedded_kind
from otto.host.kmod_kind import _kmod_kind
from otto.host.product import PRODUCT_KIND_BUILDER, Product, ShellProduct
from otto.host.shell_kind import build_declared, placeholder_values, substitute_placeholders
from otto.result import Result
from otto.utils import Status


class _Host:
    """Enough host for a plan: a default dest dir, no login home needed."""

    id = "h1"
    default_dest_dir = Path("/opt/stage")
    cached_login_home = None

    async def run(self, cmds, **kwargs):
        return Result(status=Status.Success)


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
    built = PRODUCT_KIND_BUILDER.build([_entry(install="make install")], _Host())[0]
    assert type(built) is DeclaredProduct
    assert built.install_cmd == "make install"


def test_the_default_plan_shows_the_declared_strings():
    product = PRODUCT_KIND_BUILDER.build(
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
        return Result(status=Status.Success)

    async def install(self, host):
        return Result(status=Status.Success)

    async def uninstall(self, host):
        return Result(status=Status.Success)


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
        return Result(status=Status.Success)


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


# --- build_root and {gcov_prefix_strip} (#788) ---------------------------------

_PROJECT_ROOT = Path("/home/dev/proj")


def _app_entry(kind="shell", seam="products", **params):
    """An entry named ``app`` declared by the project at ``/home/dev/proj``."""
    if kind in ("shell", "kmod", "embedded"):
        params.setdefault("artifact", "build/app.ko" if kind == "kmod" else "build/app")
    if kind == "docker_image":
        params.setdefault("image", "app:latest")
    return DeclaredEntry(
        name="app", kind=kind, seam=seam, owner="r1", base_dir=_PROJECT_ROOT, params=params
    )


def _shell(**params):
    return build_declared(_app_entry(**params), _Host(), DeclaredProduct)


_STRIP_INSTALL = "GCOV_PREFIX={cov_dir} GCOV_PREFIX_STRIP={gcov_prefix_strip} ./app"


def test_install_placeholder_is_filled_from_project_root():
    product = _shell(install=_STRIP_INSTALL)
    assert product.install_cmd == "GCOV_PREFIX=/tmp/app GCOV_PREFIX_STRIP=3 ./app"
    assert product.build_root == PurePosixPath("/home/dev/proj")
    assert product.gcov_prefix_strip == 3


def test_relative_build_root_anchors_at_base_dir():
    product = _shell(install=_STRIP_INSTALL, build_root="build/out")
    assert product.install_cmd == "GCOV_PREFIX=/tmp/app GCOV_PREFIX_STRIP=5 ./app"
    assert product.build_root == PurePosixPath("/home/dev/proj/build/out")


def test_build_root_elsewhere_overrides_project_root():
    product = _shell(install=_STRIP_INSTALL, build_root="/builds/ci/proj/build")
    assert product.install_cmd == "GCOV_PREFIX=/tmp/app GCOV_PREFIX_STRIP=4 ./app"
    assert product.build_root == PurePosixPath("/builds/ci/proj/build")


def test_dotdot_build_root_is_refused_at_load():
    with pytest.raises(ValueError, match=r"'app'.*'build_root' must not contain '\.\.'"):
        _shell(build_root="../sibling")


def test_dotdot_refusal_quotes_the_value_as_written():
    with pytest.raises(ValueError, match=r"got 'build/\.\./x'\)$") as caught:
        _shell(build_root="build/../x")
    assert "'build/../x'" in str(caught.value)
    assert "/home/dev/proj/build/../x" not in str(caught.value)


def test_empty_build_root_is_refused():
    with pytest.raises(ValueError, match="'build_root' must not be empty"):
        _shell(build_root="")


def test_uninstall_accepts_gcov_prefix_strip():
    product = _shell(uninstall="echo strip={gcov_prefix_strip}")
    assert product.uninstall_cmd == "echo strip=3"


def test_shell_and_docker_share_the_strip():
    common = {"build_root": "build"}
    shell = _shell(install="GCOV_PREFIX_STRIP={gcov_prefix_strip}", **common)
    docker = _docker_image_kind(
        _app_entry(kind="docker_image", run_args="--label s={gcov_prefix_strip}", **common),
        _Host(),
    )
    assert shell.install_cmd == "GCOV_PREFIX_STRIP=4"
    assert docker.run_args == "--label s=4"
    assert shell.build_root == docker.build_root == PurePosixPath("/home/dev/proj/build")
    assert shell.gcov_prefix_strip == docker.gcov_prefix_strip == 4


def test_shell_and_docker_default_build_root_is_the_project_root():
    shell = _shell(install="GCOV_PREFIX_STRIP={gcov_prefix_strip}")
    docker = _docker_image_kind(
        _app_entry(kind="docker_image", run_args="--label s={gcov_prefix_strip}"), _Host()
    )
    assert shell.install_cmd == "GCOV_PREFIX_STRIP=3"
    assert docker.run_args == "--label s=3"
    assert shell.build_root == docker.build_root == _PROJECT_ROOT


def _kmod_host():
    async def _verb(*args, **kwargs):
        return Result(status=Status.Success)

    return SimpleNamespace(id="h1", load=_verb, unload=_verb, lsmod=_verb)


def test_kmod_and_embedded_refuse_build_root():
    with pytest.raises(ValueError, match=r"unknown param.*build_root"):
        _kmod_kind(_app_entry(kind="kmod", build_root="build"), _kmod_host())
    with pytest.raises(ValueError, match=r"unknown param.*build_root"):
        _embedded_kind(
            _app_entry(kind="embedded", build_root="build"),
            SimpleNamespace(id="h1", loader=object()),
        )


def test_kmod_params_refuse_gcov_prefix_strip():
    with pytest.raises(ValueError, match=r"unknown placeholder.*full path"):
        _kmod_kind(_app_entry(kind="kmod", params="strip={gcov_prefix_strip}"), _kmod_host())


def test_kmod_product_strip_says_a_kmod_never_strips():
    product = _kmod_kind(_app_entry(kind="kmod"), _kmod_host())
    with pytest.raises(ValueError, match=r"'app' is a kmod: .*full path") as caught:
        _ = product.gcov_prefix_strip
    assert "set Product.build_root" not in str(caught.value)


def test_a_placeholder_without_a_value_is_refused_by_name():
    # The default `allowed` names every placeholder; a value set without a build
    # root has no strip, and the walk refuses it rather than format_map's KeyError.
    entry = _app_entry()
    values = placeholder_values(entry, None, None)
    with pytest.raises(
        ValueError, match=r"'params' has an unknown placeholder.*valid: \{cov_dir\}, \{name\} "
    ):
        substitute_placeholders(entry, "params", "x={gcov_prefix_strip}", values)


def test_dev_tool_refuses_build_root():
    with pytest.raises(ValueError, match="product-only param"):
        build_declared(_app_entry(seam="dev_tools", build_root="build"), _Host(), DeclaredProduct)
