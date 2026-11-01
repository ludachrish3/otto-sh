"""Docker reaping on a bed host: one spelling for the suite's sweeps and the bed reap.

Three decisions live here and nowhere else: which loaded hosts are container
parents (:func:`docker_parents`), what a disposable test stack's name carries
(:data:`ORPHAN_FRAGMENTS`, read by :func:`reap_docker`), and how the e2e
image tests label what they build (:data:`E2E_IMAGE_LABEL`, read by
:func:`remove_labelled_images` and its command builder). The integration
conftest's orphan sweep, the image-honesty test's finalizer, the e2e docker
pool and ``scripts/bed_reap.py`` all call in here, so the pool and the reap
cannot drift apart.
"""

from collections.abc import Iterable

from otto.logger.mode import LogMode

ORPHAN_FRAGMENTS = ("-e2e-", "-noexist-")
"""Compose-project name INFIXES that only ever appear in *disposable* test stacks.

Matched as infixes, never as a prefix, which is what let them survive the
use-case cutover unchanged: a compose project is ``<lab>-<usecase>-<suffix>``
and the e2e modules' ``fresh_suffix`` still yields ``e2e-<hex>`` /
``run-e2e-<hex>``, so ``unix-project1-e2e-1a2b3c4d`` still carries ``-e2e-``.
``tests/unit/test_docker_reaper_scope.py`` pins that agreement in the default
lane. ``-noexist-`` is retained for stacks left by older runs: nothing in the
suite mints that suffix today. Each test run mints a fresh suffix, so an
interrupted or crashed run leaves an orphan stack behind; roughly 30 orphans
exhaust docker's default address pool and wedge every subsequent ``compose
up``. Stacks matching these fragments belong to no live developer session.
"""
E2E_IMAGE_LABEL = "otto-e2e-699"
"""The label every image the e2e image tests build carries (``<label>=<token>`` per test)."""
_PROBE_TIMEOUT = 30
_REMOVE_TIMEOUT = 120


def docker_parents(hosts: Iterable[object]) -> list[str]:
    """The ids of the loaded *hosts* that can parent a container.

    A container's parent session must be ssh (``DockerContainerHost``), so a
    ``docker_capable`` host whose resolved term is not ssh is no parent: the
    resolution is otto's own, pins and preferences included, never rebuilt here.
    On the tech1 lab test2 is ``docker_capable`` with ssh among its terms but
    resolves to telnet, so the parents are test1 and test3.
    """
    from otto.docker.compose import is_docker_capable

    return [host.id for host in hosts if is_docker_capable(host) and host.term == "ssh"]


def image_ids_command(label_filter: str) -> str:
    """``docker image ls`` of every id (full, dangling included) matching *label_filter*.

    *label_filter* is the ``--filter`` value: ``label=<label>`` for every image
    carrying the label, ``label=<label>=<token>`` for one test's own.
    """
    return f"docker image ls -aq --no-trunc --filter {label_filter}"


def remove_labelled_images_command(label_filters: list[str]) -> str:
    """One shell line removing, by id, every image any of *label_filters* matches.

    The labels are the only selectors, so nothing another test owns can match;
    every filter's images are removed before any caller checks, so one failure
    leaks none of the others. Ends in ``true``: a list that matches nothing is
    not an error.
    """
    loops = "; ".join(
        f"for i in $({image_ids_command(label)}); do docker image rm -f -- $i; done"
        for label in label_filters
    )
    return f"{loops}; true"


async def _lines(host: object, command: str, timeout: float) -> list[str]:
    """Run *command* on *host*; its exact output lines, or :class:`ProbeFailedError`."""
    # The hygiene oracle imports this module's constants for its probes, so its
    # status check and line splitter are bound here at call time, not at import.
    from tests._fixtures.bed_hygiene import check_probe_result, probe_lines

    result = await host.exec(command, timeout=timeout, log=LogMode.QUIET)  # type: ignore[attr-defined]
    check_probe_result(getattr(host, "id", "<unknown host>"), result)
    return [line for line in probe_lines(result.value) if line]


async def reap_docker(host: object) -> list[str]:
    """Remove every container and network on *host* whose name carries an orphan fragment.

    Containers first (a network with a live container cannot go), then
    networks, per fragment; every command's result is checked, so a removal
    that failed is an error, never a clean read. Returns what it removed, as
    ``container <id>`` / ``network <id>`` lines.
    """
    removed: list[str] = []
    for fragment in ORPHAN_FRAGMENTS:
        containers = await _lines(host, f"docker ps -aq --filter 'name={fragment}'", _PROBE_TIMEOUT)
        if containers:
            await _lines(host, f"docker rm -f {' '.join(containers)}", _REMOVE_TIMEOUT)
            removed += [f"container {c}" for c in containers]
        networks = await _lines(
            host, f"docker network ls -q --filter 'name={fragment}'", _PROBE_TIMEOUT
        )
        if networks:
            await _lines(host, f"docker network rm {' '.join(networks)}", _REMOVE_TIMEOUT)
            removed += [f"network {n}" for n in networks]
    return removed


async def remove_labelled_images(host: object, label: str) -> list[str]:
    """Remove every image on *host* carrying *label*; return the ids removed.

    Verified after the removal: an id still listed under the label is raised,
    not returned as removed.
    """
    label_filter = f"label={label}"
    ids = await _lines(host, image_ids_command(label_filter), _PROBE_TIMEOUT)
    if not ids:
        return []
    await _lines(host, remove_labelled_images_command([label_filter]), _REMOVE_TIMEOUT)
    left = await _lines(host, image_ids_command(label_filter), _PROBE_TIMEOUT)
    if left:
        raise RuntimeError(f"{getattr(host, 'id', host)}: images still carry {label}: {left}")
    return ids
