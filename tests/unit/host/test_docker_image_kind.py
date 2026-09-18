"""The built-in ``docker_image`` kind: params, verbs, hooks, container logs."""

import shlex
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from otto.declared import DeclaredEntry
from otto.host import docker_image_kind  # noqa: F401 — import registers the kind
from otto.host import product as product_mod
from otto.host.docker_image_kind import DockerImageProduct, _loaded_reference
from otto.result import CommandResult, NotRunResult, Result, Results
from otto.utils import Status


def _entry(**params):
    params.setdefault("image", "docker/app.tar")
    return DeclaredEntry(
        name=params.pop("name", "app"),
        kind="docker_image",
        seam="products",
        owner="r",
        base_dir=Path("/repo"),
        match={},
        params=params,
    )


class _DockerHost(SimpleNamespace):
    """A host double whose exec()/run() answer by command substring; records everything.

    An answer of ``Status.NotRun`` yields a real :class:`~otto.result.NotRunResult` (whose
    ``.value`` raises on read) — the same shape a dry-run session's ``exec``/``run`` produce —
    so a test proves a decline is never read, not merely mapped.
    """

    def __init__(self, answers=None, **attrs):
        super().__init__(**attrs)
        self.id = attrs.get("id", "test3")
        self.answers = dict(answers or {})
        self.exec_calls: list[str] = []
        self.run_calls: list[tuple[str, dict]] = []
        self.put = AsyncMock(return_value=Result(Status.Success, value={}))
        self.get = AsyncMock(return_value=Result(Status.Success, value={}))

    def _answer(self, cmd: str) -> tuple[Status, str]:
        for needle, (status, output) in self.answers.items():
            if needle in cmd:
                return status, output
        return Status.Success, ""

    async def exec(self, cmd, **kw):
        self.exec_calls.append(cmd)
        status, output = self._answer(cmd)
        if status is Status.NotRun:
            return NotRunResult(status=Status.NotRun, command=cmd, retcode=-1, host_name=self.id)
        retcode = 0 if status is Status.Success else 1
        return CommandResult(status, value=output, command=cmd, retcode=retcode)

    async def run(self, cmd, **kw):
        self.run_calls.append((cmd, kw))
        status, output = self._answer(cmd)
        if status is Status.NotRun:
            entry = NotRunResult(status=Status.NotRun, command=cmd, retcode=-1, host_name=self.id)
        else:
            retcode = 0 if status is Status.Success else 1
            entry = CommandResult(command=cmd, value=output, status=status, retcode=retcode)
        return Results.collect([entry])


def _build(host=None, **params) -> DockerImageProduct:
    return product_mod.PRODUCT_KINDS.get("docker_image")(_entry(**params), host or _DockerHost())


# ── builder ──────────────────────────────────────────────────────────────────


def test_docker_image_is_a_product_kind_only():
    from otto.host.dev_tool import DEV_TOOL_KINDS

    assert "docker_image" in product_mod.PRODUCT_KINDS
    assert "docker_image" not in DEV_TOOL_KINDS


def test_a_tarball_image_is_anchored_both_forms_are_unknown_until_declared():
    tar = _build(image="docker/app.tar")
    assert tar.is_tarball
    assert tar.artifact == Path("/repo/docker/app.tar")
    # Both forms override instrumented() to never scan `artifact` at all
    # (see test_reference_instrumented_never_scans_the_cwd below for proof).
    assert tar.instrumented() is None
    ref = _build(image="registry.example/app:1.2")
    assert not ref.is_tarball
    assert ref.image == "registry.example/app:1.2"
    assert ref.instrumented() is None
    assert _build(image="registry.example/app:1.2", instrumented=True).instrumented() is True


def test_reference_instrumented_never_scans_the_cwd(monkeypatch):
    # Prove the override itself, not merely its usual effect: force the
    # inherited scan to answer True and show instrumented() still ignores
    # it for both forms — a reference's `artifact` is a bare, CWD-relative
    # Path built only to satisfy ShellProduct's constructor, never a
    # property of the declared image, so scanning it would be meaningless
    # (and, if something of that name exists in otto's CWD, wrong).
    monkeypatch.setattr(product_mod, "scan_for_instrumentation", lambda _path: True)
    assert _build(image="registry.example/app:1.2").instrumented() is None
    assert _build(image="docker/app.tar").instrumented() is None


def test_defaults_and_declared_params():
    p = _build(image="app:latest")
    assert (p.pull, p.run_args, p.container_name, p.loaded_ref) == (False, "", "app", None)
    q = _build(
        image="app:latest",
        pull=True,
        run_args="-e GCOV_PREFIX={cov_dir} --net host",
        container_name="c1",
        cov_dir="/var/cov/app",
    )
    expected = (True, "-e GCOV_PREFIX=/var/cov/app --net host", "c1")
    assert (q.pull, q.run_args, q.container_name) == expected


def test_run_args_expands_the_name_placeholder_too():
    p = _build(image="app:latest", run_args="--label app={name} --label cov={cov_dir}")
    assert p.run_args == "--label app=app --label cov=/tmp/app"


_UNKNOWN_PARAM_FRAGMENT = (
    "kind 'docker_image' got unknown param(s): ['bogus']; valid: "
    "image, pull, run_args, container_name, cov_dir, instrumented, debug_log_globs"
)


@pytest.mark.parametrize(
    ("params", "fragment"),
    [
        ({"image": ""}, "'image' must not be empty"),
        ({"pull": "yes"}, "'pull' must be a bool"),
        ({"run_args": "{typo}"}, "unknown placeholder"),
        ({"cov_dir": ""}, "'cov_dir' must not be empty"),
        ({"pull": True}, "'pull' only applies to a reference image, not a tarball"),
        ({"bogus": 1}, _UNKNOWN_PARAM_FRAGMENT),
    ],
)
def test_rejects_bad_params_naming_the_entry(params, fragment):
    with pytest.raises(ValueError, match=r"\[\[products\]\] 'app'") as ei:
        _build(**params)
    assert fragment in str(ei.value)


def test_requires_an_image_param():
    entry = DeclaredEntry(
        name="app",
        kind="docker_image",
        seam="products",
        owner="r",
        base_dir=Path("/repo"),
        match={},
        params={},
    )
    with pytest.raises(ValueError, match=r"requires an 'image' param"):
        product_mod.PRODUCT_KINDS.get("docker_image")(entry, _DockerHost())


def test_loaded_reference_returns_none_for_unrecognized_output():
    assert _loaded_reference("nothing useful\n") is None


# ── verbs ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_refuses_a_host_without_docker():
    host = _DockerHost(answers={"command -v docker": (Status.Error, "")})
    result = await _build(host, image="app:1").install(host)
    assert not result.is_ok
    assert "docker is not on test3" in result.msg


@pytest.mark.asyncio
async def test_tarball_install_stages_loads_then_runs_with_the_bind_mount():
    host = _DockerHost(answers={"docker load": (Status.Success, "Loaded image: app:1.2\n")})
    p = _build(
        host,
        image="docker/app.tar",
        cov_dir="/var/cov/app",
        run_args="-e GCOV_PREFIX={cov_dir}",
    )
    assert (await p.stage(host)).is_ok
    host.put.assert_awaited_once_with(Path("/repo/docker/app.tar"), Path("/tmp"))
    assert (await p.install(host)).is_ok
    assert p.loaded_ref == "app:1.2"
    assert "docker load -i /tmp/app.tar" in host.exec_calls[1]
    # The staged tarball is removed right after a successful load — it is an
    # intermediate docker load has already consumed, not the product itself.
    assert host.exec_calls[2] == "rm -f /tmp/app.tar"
    assert host.exec_calls[-1] == (
        "docker run -d --name app -v /var/cov/app:/var/cov/app -e GCOV_PREFIX=/var/cov/app app:1.2"
    )


@pytest.mark.asyncio
async def test_tarball_load_without_a_tag_runs_by_image_id():
    host = _DockerHost(
        answers={"docker load": (Status.Success, "Loaded image ID: sha256:abc123\n")}
    )
    p = _build(host, image="docker/app.tar")
    await p.install(host)
    assert p.loaded_ref == "sha256:abc123"
    assert host.exec_calls[-1].endswith(" sha256:abc123")


@pytest.mark.asyncio
async def test_tarball_load_failure_names_the_command():
    host = _DockerHost(answers={"docker load": (Status.Error, "no space left on device")})
    result = await _build(host, image="docker/app.tar").install(host)
    assert not result.is_ok
    assert "docker load failed on test3" in result.msg


@pytest.mark.asyncio
async def test_tarball_load_with_no_recognizable_reference_fails():
    host = _DockerHost(answers={"docker load": (Status.Success, "nothing useful\n")})
    result = await _build(host, image="docker/app.tar").install(host)
    assert not result.is_ok
    assert "docker load reported no image on test3" in result.msg


@pytest.mark.asyncio
async def test_docker_run_failure_names_the_command():
    host = _DockerHost(answers={"docker run": (Status.Error, "port is already allocated")})
    result = await _build(host, image="app:1").install(host)
    assert not result.is_ok
    assert "docker run failed on test3" in result.msg


@pytest.mark.asyncio
async def test_reference_without_pull_must_already_be_present():
    absent = _DockerHost(answers={"docker image inspect": (Status.Error, "No such image")})
    result = await _build(absent, image="app:1").install(absent)
    assert not result.is_ok
    assert "app:1 is not present on test3" in result.msg
    assert "pull = true" in result.msg
    assert not any(c.startswith("docker run") for c in absent.exec_calls)
    present = _DockerHost()
    assert (await _build(present, image="app:1").install(present)).is_ok
    assert not any("docker pull" in c for c in present.exec_calls)
    assert present.exec_calls[-1] == "docker run -d --name app -v /tmp/app:/tmp/app app:1"


@pytest.mark.asyncio
async def test_reference_with_pull_pulls_first():
    host = _DockerHost()
    assert (await _build(host, image="app:1", pull=True).install(host)).is_ok
    assert host.exec_calls[1] == "docker pull app:1"
    failing = _DockerHost(answers={"docker pull": (Status.Error, "denied")})
    result = await _build(failing, image="app:1", pull=True).install(failing)
    assert not result.is_ok
    assert "docker pull app:1 failed" in result.msg


@pytest.mark.asyncio
async def test_stage_is_a_noop_for_a_reference():
    host = _DockerHost()
    assert (await _build(host, image="app:1").stage(host)).is_ok
    host.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_is_installed_means_the_container_is_running():
    running = _DockerHost(answers={"docker container inspect": (Status.Success, "true\n")})
    assert await _build(running, image="app:1").is_installed(running) is True
    assert "docker container inspect -f '{{.State.Running}}' app" in running.exec_calls[0]
    stopped = _DockerHost(answers={"docker container inspect": (Status.Success, "false\n")})
    assert await _build(stopped, image="app:1").is_installed(stopped) is False
    missing = _DockerHost(answers={"docker container inspect": (Status.Error, "No such object")})
    assert await _build(missing, image="app:1").is_installed(missing) is False


@pytest.mark.asyncio
async def test_uninstall_removes_the_container_and_only_a_loaded_image():
    host = _DockerHost(
        answers={
            "docker load": (Status.Success, "Loaded image: app:1\n"),
            "docker container inspect -f '{{.Config.Image}}'": (Status.Success, "app:1\n"),
        }
    )
    p = _build(host, image="docker/app.tar")
    await p.install(host)
    assert (await p.uninstall(host)).is_ok
    assert host.exec_calls[-3:] == [
        "docker container inspect -f '{{.Config.Image}}' app",
        "docker rm -f app",
        "docker rmi app:1",
    ]
    ref_host = _DockerHost()
    assert (await _build(ref_host, image="app:1").uninstall(ref_host)).is_ok
    assert ref_host.exec_calls == ["docker rm -f app"]


@pytest.mark.asyncio
async def test_uninstall_resolves_the_image_from_the_container_not_from_memory():
    """B2's realistic case: a FRESH instance (never install()-ed itself, so
    ``loaded_ref`` is None throughout) uninstalls what a separate `otto
    install` process left running — exactly the shape of `otto install` then
    `otto uninstall` as two processes. The image must come from the
    container, never from in-memory state."""
    host = _DockerHost(
        answers={"docker container inspect -f '{{.Config.Image}}'": (Status.Success, "app:1\n")}
    )
    p = _build(host, image="docker/app.tar")
    assert p.loaded_ref is None
    result = await p.uninstall(host)
    assert result.is_ok
    assert host.exec_calls == [
        "docker container inspect -f '{{.Config.Image}}' app",
        "docker rm -f app",
        "docker rmi app:1",
    ]


@pytest.mark.asyncio
async def test_uninstall_skips_rmi_when_the_container_is_already_gone():
    # Falls back to nothing, not to loaded_ref: an absent container has
    # nothing to resolve an image from, so docker rm -f's own no-op below
    # is the end of it — no rmi is attempted.
    host = _DockerHost(
        answers={
            "docker container inspect -f '{{.Config.Image}}'": (
                Status.Error,
                "No such object: app",
            ),
            "docker rm -f": (Status.Error, "Error: No such container: app"),
        }
    )
    result = await _build(host, image="docker/app.tar").uninstall(host)
    assert result.is_ok
    assert host.exec_calls == [
        "docker container inspect -f '{{.Config.Image}}' app",
        "docker rm -f app",
    ]


@pytest.mark.asyncio
async def test_uninstall_tolerates_an_already_removed_container():
    # Idempotent: the bed lane re-runs uninstall after a failed install, when
    # the container was never created (or a prior run already removed it).
    host = _DockerHost(answers={"docker rm -f": (Status.Error, "Error: No such container: app")})
    result = await _build(host, image="app:1").uninstall(host)
    assert result.is_ok
    assert host.exec_calls == ["docker rm -f app"]


@pytest.mark.asyncio
async def test_uninstall_rm_failure_names_the_command():
    host = _DockerHost(answers={"docker rm -f": (Status.Error, "connection refused")})
    result = await _build(host, image="app:1").uninstall(host)
    assert not result.is_ok
    assert "docker rm -f app failed on test3" in result.msg


@pytest.mark.asyncio
async def test_uninstall_rmi_failure_names_the_command():
    host = _DockerHost(
        answers={
            "docker load": (Status.Success, "Loaded image: app:1\n"),
            "docker container inspect -f '{{.Config.Image}}'": (Status.Success, "app:1\n"),
            "docker rmi": (Status.Error, "image is being used by a container"),
        }
    )
    p = _build(host, image="docker/app.tar")
    await p.install(host)
    result = await p.uninstall(host)
    assert not result.is_ok
    assert "docker rmi app:1 failed on test3" in result.msg


@pytest.mark.asyncio
async def test_injection_shaped_values_are_quoted_not_executed():
    nasty_image = "ref's; sneaky image"
    nasty_name = "a b's; rm -rf"
    nasty_cov = "/opt/My App's; cov"
    host = _DockerHost()
    p = _build(host, image=nasty_image, container_name=nasty_name, cov_dir=nasty_cov)
    assert (await p.install(host)).is_ok
    expected_run = (
        f"docker run -d --name {shlex.quote(nasty_name)} "
        f"-v {shlex.quote(nasty_cov)}:{shlex.quote(nasty_cov)} {shlex.quote(nasty_image)}"
    )
    assert host.exec_calls[-1] == expected_run
    assert (await p.reset_coverage(host)).is_ok
    expected_delete = f"find {shlex.quote(nasty_cov)} -name '*.gcda' -type f -delete"
    assert host.run_calls[-1] == (expected_delete, {"sudo": True})


# ── dry-run declines propagate, never crash and never become Error ─────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("build_kwargs", "declined_substring"),
    [
        ({"image": "app:1"}, "command -v docker"),
        ({"image": "docker/app.tar"}, "docker load"),
        ({"image": "app:1", "pull": True}, "docker pull"),
        ({"image": "app:1"}, "docker image inspect"),
        ({"image": "app:1"}, "docker run"),
    ],
)
async def test_install_propagates_a_dry_run_decline_at_every_probe(
    build_kwargs, declined_substring
):
    host = _DockerHost(answers={declined_substring: (Status.NotRun, "")})
    result = await _build(host, **build_kwargs).install(host)
    assert result.status is Status.NotRun


@pytest.mark.asyncio
async def test_uninstall_propagates_a_dry_run_decline_on_rm():
    host = _DockerHost(answers={"docker rm -f": (Status.NotRun, "")})
    result = await _build(host, image="app:1").uninstall(host)
    assert result.status is Status.NotRun


@pytest.mark.asyncio
async def test_uninstall_propagates_a_dry_run_decline_on_rmi():
    host = _DockerHost(
        answers={
            "docker load": (Status.Success, "Loaded image: app:1\n"),
            "docker container inspect -f '{{.Config.Image}}'": (Status.Success, "app:1\n"),
            "docker rmi": (Status.NotRun, ""),
        }
    )
    p = _build(host, image="docker/app.tar")
    await p.install(host)
    result = await p.uninstall(host)
    assert result.status is Status.NotRun


@pytest.mark.asyncio
async def test_uninstall_propagates_a_dry_run_decline_on_inspect():
    host = _DockerHost(
        answers={"docker container inspect -f '{{.Config.Image}}'": (Status.NotRun, "")}
    )
    result = await _build(host, image="docker/app.tar").uninstall(host)
    assert result.status is Status.NotRun


@pytest.mark.asyncio
async def test_is_installed_does_not_crash_on_a_dry_run_decline():
    host = _DockerHost(answers={"docker container inspect": (Status.NotRun, "")})
    assert await _build(host, image="app:1").is_installed(host) is False


@pytest.mark.asyncio
async def test_prepare_coverage_never_contacts_a_declining_host():
    host = _DockerHost(answers={"docker": (Status.NotRun, "")})
    result = await _build(host, image="app:1").prepare_coverage(host)
    assert result.is_ok
    assert host.exec_calls == []
    assert host.run_calls == []


@pytest.mark.asyncio
async def test_reset_coverage_propagates_a_dry_run_decline():
    host = _DockerHost(answers={"find": (Status.NotRun, "")})
    result = await _build(host, image="app:1").reset_coverage(host)
    assert result.status is Status.NotRun


@pytest.mark.asyncio
async def test_debug_logs_propagates_a_dry_run_decline(tmp_path):
    host = _DockerHost(answers={"docker logs": (Status.NotRun, "")})
    result = await _build(host, image="app:1").get_debug_logs(host, tmp_path)
    assert result.status is Status.NotRun


# ── hooks and logs ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_coverage_deletes_under_sudo():
    host = _DockerHost()
    p = _build(host, image="app:1", cov_dir="/var/cov/app")
    assert (await p.reset_coverage(host)).is_ok
    assert host.run_calls == [("find /var/cov/app -name '*.gcda' -type f -delete", {"sudo": True})]
    assert host.exec_calls == []


@pytest.mark.asyncio
async def test_reset_coverage_failure_names_the_product():
    host = _DockerHost(answers={"find": (Status.Error, "permission denied")})
    result = await _build(host, image="app:1", cov_dir="/var/cov/app").reset_coverage(host)
    assert not result.is_ok
    assert "deleting .gcda under /var/cov/app failed: permission denied" in result.msg


@pytest.mark.asyncio
async def test_debug_logs_add_container_log_to_the_haul(tmp_path):
    host = _DockerHost(answers={"docker logs": (Status.Success, "hello from the container\n")})
    p = _build(host, image="app:1")
    result = await p.get_debug_logs(host, tmp_path)
    assert result.is_ok
    assert (tmp_path / "container.log").read_text() == "hello from the container\n"
    assert "docker logs app" in host.exec_calls[-1]
    host.get.assert_not_awaited()  # no debug_log_globs declared


@pytest.mark.asyncio
async def test_debug_logs_hauls_the_declared_globs_too(tmp_path):
    host = _DockerHost(answers={"docker logs": (Status.Success, "hello\n")})
    p = _build(host, image="app:1", debug_log_globs=["/var/log/app.log"])
    result = await p.get_debug_logs(host, tmp_path)
    assert result.is_ok
    host.get.assert_awaited_once_with([Path("/var/log/app.log")], tmp_path)
    assert (tmp_path / "container.log").read_text() == "hello\n"


@pytest.mark.asyncio
async def test_debug_logs_failure_is_reported_when_the_declared_haul_succeeded(tmp_path):
    host = _DockerHost(answers={"docker logs": (Status.Error, "no such container")})
    result = await _build(host, image="app:1").get_debug_logs(host, tmp_path)
    assert not result.is_ok
    assert "docker logs app failed on test3" in result.msg


@pytest.mark.asyncio
async def test_debug_logs_reports_the_haul_failure_even_when_docker_logs_also_fails(tmp_path):
    host = _DockerHost(answers={"docker logs": (Status.Error, "no such container")})
    host.get = AsyncMock(return_value=Result(Status.Error, msg="fetch failed"))
    p = _build(host, image="app:1", debug_log_globs=["/var/log/app.log"])
    result = await p.get_debug_logs(host, tmp_path)
    assert not result.is_ok
    assert result.msg == "fetch failed"
