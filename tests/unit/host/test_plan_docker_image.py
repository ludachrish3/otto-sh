"""The docker_image kind's plan is what its hooks do (spec §3)."""

from pathlib import Path

import pytest

from otto.declared import DeclaredEntry
from otto.host import docker_image_kind  # noqa: F401 — import registers the kind
from otto.host.product import LOGIN_HOME_PLACEHOLDER, PRODUCT_KIND_BUILDER

DOCKER_PROBE = "command -v docker"


def _image(host, image, **params):
    entry = DeclaredEntry(
        name="web",
        kind="docker_image",
        seam="products",
        owner="r1",
        base_dir=Path("/sut"),
        match={},
        params={"image": image, **params},
    )
    return PRODUCT_KIND_BUILDER.build([entry], host)[0]


def _actions(lines, reads):
    return [line for line in lines if line not in reads]


@pytest.mark.asyncio
async def test_reference_with_pull_plans_pull_then_run(plan_recorder):
    host = plan_recorder
    host.script(DOCKER_PROBE, "/usr/bin/docker")
    product = _image(host, "nginx:1.25", pull=True, run_args="-p 80:80")
    plan = product.plan(host)
    assert (await product.stage(host)).is_ok
    assert host.take() == [] == plan.stage
    assert (await product.install(host)).is_ok
    assert (
        _actions(host.take(), {DOCKER_PROBE})
        == plan.install
        == [
            "docker pull nginx:1.25",
            "docker run -d --name web -v /tmp/web:/tmp/web -p 80:80 nginx:1.25",
        ]
    )
    assert (await product.uninstall(host)).is_ok
    assert host.take() == plan.uninstall == ["docker rm -f web"]
    assert plan.unchecked == ["that docker is on h1's PATH (command -v docker)"]


@pytest.mark.asyncio
async def test_reference_without_pull_plans_run_and_names_the_presence_read(plan_recorder):
    host = plan_recorder
    host.script(DOCKER_PROBE, "/usr/bin/docker")
    product = _image(host, "nginx:1.25")
    plan = product.plan(host)
    assert (await product.install(host)).is_ok
    present = "docker image inspect nginx:1.25 >/dev/null"
    recorded = host.take()
    assert present in recorded
    assert (
        _actions(recorded, {DOCKER_PROBE, present})
        == plan.install
        == ["docker run -d --name web -v /tmp/web:/tmp/web nginx:1.25"]
    )
    assert plan.unchecked == [
        "that docker is on h1's PATH (command -v docker)",
        (
            "that nginx:1.25 is present on h1 (docker image inspect); a missing image fails "
            "the install unless the entry sets `pull = true`"
        ),
    ]


@pytest.mark.asyncio
async def test_tarball_plans_load_cleanup_run_and_the_image_reads(plan_recorder):
    host = plan_recorder
    host.default_dest_dir = Path("/opt/stage")
    host.script(DOCKER_PROBE, "/usr/bin/docker")
    host.script("docker load -i /opt/stage/web.tar", "Loaded image: web:1\n")
    host.script("docker container inspect -f '{{.Config.Image}}' web", "web:1\n")
    product = _image(host, "images/web.tar")
    plan = product.plan(host)
    assert (await product.stage(host)).is_ok
    assert host.take() == plan.stage == ["PUT /sut/images/web.tar -> /opt/stage"]
    assert (await product.install(host)).is_ok
    assert (
        _actions(host.take(), {DOCKER_PROBE})
        == [line.replace("<loaded image>", "web:1") for line in plan.install]
        == [
            "docker load -i /opt/stage/web.tar",
            "rm -f /opt/stage/web.tar",
            "docker run -d --name web -v /tmp/web:/tmp/web web:1",
        ]
    )
    assert (await product.uninstall(host)).is_ok
    inspect = "docker container inspect -f '{{.Config.Image}}' web"
    assert (
        _actions(host.take(), {inspect})
        == [line.replace("<image the container ran>", "web:1") for line in plan.uninstall]
        == ["docker rm -f web", "docker rmi web:1"]
    )
    assert plan.unchecked == [
        "that docker is on h1's PATH (command -v docker)",
        "the reference `docker load` prints, which `docker run` then names (<loaded image>)",
        (
            "the image to remove, read from the container (docker container inspect web); "
            "no running container means no `docker rmi`"
        ),
    ]


def test_tarball_plan_uses_the_placeholder_in_every_line(plan_recorder):
    host = plan_recorder  # no default_dest_dir: the tarball lands in the login home
    plan = _image(host, "images/web.tar").plan(host)
    assert plan.stage == [f"PUT /sut/images/web.tar -> {LOGIN_HOME_PLACEHOLDER}"]
    assert plan.install[0] == f"docker load -i '{LOGIN_HOME_PLACEHOLDER}/web.tar'"
    assert plan.install[1] == f"rm -f '{LOGIN_HOME_PLACEHOLDER}/web.tar'"
    assert plan.unchecked[0] == (
        "the login home — product 'web' declares no stage_dir and h1 no default_dest_dir"
    )
    assert host.home_reads == 0


def test_a_tarball_on_a_host_with_no_login_home_is_a_refusal():
    class _NoHome:
        id = "board1"

    plan = _image(_NoHome(), "images/web.tar").plan(_NoHome())
    assert plan.stage == plan.install == plan.uninstall == []
    assert plan.unchecked == [
        (
            "product 'web' declares no stage_dir and board1 no default_dest_dir, and board1 "
            "has no login home to fall back on: the install is refused"
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["pull", "present", "tarball"])
async def test_plan_quotes_exactly_as_the_hooks_do_for_values_that_need_it(plan_recorder, form):
    host = plan_recorder
    host.default_dest_dir = Path("/opt/st age")
    host.script(DOCKER_PROBE, "/usr/bin/docker")
    host.script("docker load -i '/opt/st age/my web.tar'", "Loaded image: web:1\n")
    host.script("docker container inspect -f '{{.Config.Image}}' 'we b'", "web:1\n")
    image = "images/my web.tar" if form == "tarball" else "my reg/nginx:1.25"
    product = _image(
        host,
        image,
        pull=form == "pull",
        run_args="-p 80:80 -e 'A=b c'",
        container_name="we b",
        cov_dir="/opt/My App/cov",
    )
    plan = product.plan(host)
    reads = {
        DOCKER_PROBE,
        "docker image inspect 'my reg/nginx:1.25' >/dev/null",
        "docker container inspect -f '{{.Config.Image}}' 'we b'",
    }
    assert (await product.stage(host)).is_ok
    assert host.take() == plan.stage
    assert (await product.install(host)).is_ok
    install = _actions(host.take(), reads)
    assert (await product.uninstall(host)).is_ok
    uninstall = _actions(host.take(), reads)
    if form == "tarball":
        install = [line.replace("web:1", "<loaded image>") for line in install]
        uninstall = [line.replace("web:1", "<image the container ran>") for line in uninstall]
    assert install == plan.install
    assert uninstall == plan.uninstall
    assert "'we b'" in plan.uninstall[0]
    assert "-v '/opt/My App/cov':'/opt/My App/cov' -p 80:80 -e 'A=b c'" in plan.install[-1]
