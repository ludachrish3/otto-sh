"""Fixtures for coverage integration tests.

These tests require:
- Vagrant test VMs (test1/test2) to be running
- gcc and lcov installed on the dev VM
"""

import asyncio
import contextlib
import json
import os
import re
import socket
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from otto.config.env import SUT_DIRS_ENV_VAR
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from tests._fixtures.labdata import flatten_lab_doc, lab_data_path
from tests._fixtures.paths import default_sut_dir
from tests.conftest import BUSYBOX_BED_GROUP, BUSYBOX_PARAM_TOKENS

_INTEGRATION_ROOT = Path(__file__).parent
_BUSYBOX_BED_ROOT = _INTEGRATION_ROOT / "busybox_bed"

# The bracketed parametrize id at the end of a nodeid, with or without the
# ``@group`` suffix xdist appends to it. Written as one pattern because both
# halves of the guard below need the same answer about where the id ends: a
# nodeid is ``file::test[a-b]`` or ``file::test[a-b]@group``, and the id
# itself never contains a bracket.
_NODEID_ID = re.compile(r"\[([^\[\]]*)\](?:@[^\[\]]*)?$")


def pytest_collection_modifyitems(config, items):
    """Auto-apply the ``integration`` marker to every test under this tree.

    The ``tests/integration/`` directory is the single source of truth for the
    integration tier (Spec §5.1): tests here drive the real Vagrant/QEMU bed via
    otto's Python API. Stamping the marker from the path lets the marker-based
    gates (``coverage-unix`` = ``-m "integration and not embedded"``, etc.)
    select this tree without each test repeating ``@pytest.mark.integration``.
    Idempotent and additive — explicit ``embedded``/``hops``/``stability`` stay.

    NOTE (lane consequence, recorded at Wave 16): because only explicit marks
    survive on top of the path stamp, a subtree with NO ``embedded`` marks
    rides the unix lane wholesale — ``tests/integration/cov/`` is the live
    case: every module there lands in ``M_UNIX`` (``integration and not
    embedded ...``) and is invisible to ``make coverage-embedded``, including
    the pure-git/tmp_path modules that need no bed at all. That is currently
    intended (the cov integration tier exercises host tooling, not Zephyr),
    but it is a *default*, not a decision each file re-makes — a future
    embedded-coverage integration test must OPT IN with an explicit
    ``@pytest.mark.embedded`` or it will silently run (and fail) in the unix
    lane. Only ``tests/integration/host/`` distinguishes the lanes today,
    via per-param marks.
    """
    for item in items:
        if _INTEGRATION_ROOT in item.path.parents:
            item.add_marker("integration")


def _default_sut_dirs_env_impl():
    """``OTTO_SUT_DIRS`` -> the ``repo1`` fixture SUT for this tree, at RUNTIME.

    History: this was a module-scope ``ensure_sut_dirs()`` call justified by
    a "config reads OTTO_SUT_DIRS at import time" comment that stopped being
    true — every reader (``bootstrap()``/``OttoEnvSettings``, spawned otto
    subprocesses) reads the env lazily at call time, which a session-start
    write fully precedes. Import-time env writes are banned (G11): they run
    behind the root conftest's hermeticity strip's back, invisible to
    monkeypatch and to any pin that never imports this tree's conftest —
    which is how this one went uncertified for a year. ``setdefault`` +
    restore keeps a pre-set value (another harness layer) authoritative.

    Plain generator (the ``_hygiene_bracket_impl`` pattern) so the unit-lane
    pin can drive set-and-restore directly.
    """
    prior = os.environ.get(SUT_DIRS_ENV_VAR)
    os.environ.setdefault(SUT_DIRS_ENV_VAR, default_sut_dir())
    yield
    if prior is None:
        os.environ.pop(SUT_DIRS_ENV_VAR, None)
    else:
        os.environ[SUT_DIRS_ENV_VAR] = prior


@pytest.fixture(scope="session", autouse=True)
def _default_sut_dirs_env():
    yield from _default_sut_dirs_env_impl()


_LAB_DATA = lab_data_path()

# Docker host the e2e/compose tests target (test VM "test3" / test3).
_DOCKER_HOST_IP = "10.10.200.13"

# Compose-project name fragments that only ever appear in *disposable* test
# stacks: `fresh_suffix` yields ``e2e-<hex>`` and the unstarted-container test
# uses ``noexist-<hex>``. Each test run mints a fresh suffix, so an interrupted
# or crashed run leaves an orphan stack behind. Roughly 30 orphans exhaust
# docker's default address-pool ("all predefined address pools have been fully
# subnetted") and wedge every subsequent ``compose up``. Stacks matching these
# fragments are always safe to reap — they belong to no live developer session.
_ORPHAN_PROJECT_FRAGMENTS = ("-e2e-", "-noexist-")


async def _reap_orphan_docker_stacks() -> None:
    """Remove leaked ``otto-*-{e2e,noexist}-*`` containers and networks on the
    docker host so address-pool exhaustion can't accumulate across runs."""
    host = UnixHost(
        ip=_DOCKER_HOST_IP,
        element="test3",
        creds=[Cred(login="vagrant", password="vagrant")],
        is_virtual=True,
        term="ssh",
        transfer="scp",
        docker_capable=True,
    )
    try:
        for frag in _ORPHAN_PROJECT_FRAGMENTS:
            containers = (
                await host.exec(f"docker ps -aq --filter 'name={frag}'", timeout=30)
            ).value.split()
            if containers:
                await host.exec(f"docker rm -f {' '.join(containers)}", timeout=60)
            networks = (
                await host.exec(f"docker network ls -q --filter 'name={frag}'", timeout=30)
            ).value.split()
            if networks:
                await host.exec(f"docker network rm {' '.join(networks)}", timeout=60)
    finally:
        await host.close()


_DOCKER_GROUP = "docker_e2e"


def _declares_docker(item: pytest.Item) -> bool:
    """Whether *item* carries the docker modules' ``xdist_group("docker_e2e")`` mark.

    Both of xdist's spellings, ``xdist_group("docker_e2e")`` and
    ``xdist_group(name="docker_e2e")``: a keyword-form declaration that this
    read as "not in the group" would trip the premise assertion below on a
    module that is, in fact, correctly declared.
    """
    return any(
        (mark.args[:1] or (mark.kwargs.get("name"),))[0] == _DOCKER_GROUP
        for mark in item.iter_markers(name="xdist_group")
    )


@pytest.fixture(scope="session")
def reap_orphan_docker_stacks(request: pytest.FixtureRequest) -> None:
    """Sweep orphaned docker test stacks once, before the first test that DRIVES docker.

    Requested by name from the four docker modules (their ``pytestmark``
    carries ``usefixtures("reap_orphan_docker_stacks")`` next to
    ``xdist_group("docker_e2e")``), never autouse. It was autouse at session
    scope, keyed on directory membership: running ANY test under
    ``tests/integration/`` — 27 of 31 files never mention docker — reaped
    the shared host's ``-e2e-`` stacks, including a concurrent developer's
    live ones (ledger 2026-08-16: a pure-git ``cov/`` module triggered it
    twice). The blast radius is now keyed on need.

    The premise is ASSERTED, not assumed, because a narrowed-but-unasserted
    fixture drifts wrong again the moment the subtree grows: every collected
    item that requests this fixture must be in the docker xdist group. An
    offender errors this session before anything is reaped — a loud failure
    naming the test, instead of a quiet reap on someone else's stacks.
    ``tests/unit/test_docker_reaper_scope.py`` holds the same rule
    statically in the default lane, where this tree never runs.

    A stack leaked by an earlier interrupted run would otherwise compound
    until the daemon runs out of network subnets. Best-effort — if the host
    is unreachable we let the individual tests report that themselves.

    Hermetic venues (e.g. the GitHub ``chaos-tier2`` nightly job) have no
    route to the bed at all — unlike a reachable-but-docker-broken host,
    connecting to an unroutable address can hang on SYN retries well past
    the point where the `contextlib.suppress` below would ever catch
    anything, stalling the whole session before a single test runs. A short
    TCP preflight distinguishes "no route" (skip the reap silently — it's a
    lab-only courtesy, not a test, so nothing here fails or skips a test)
    from "host up, something else went wrong" (still attempt the reap and
    suppress as before).
    """
    offenders = [
        item.nodeid
        for item in request.session.items
        if "reap_orphan_docker_stacks" in getattr(item, "fixturenames", ())
        and not _declares_docker(item)
    ]
    assert not offenders, (
        f"reap_orphan_docker_stacks was requested by {len(offenders)} test(s) that do not "
        f"declare xdist_group({_DOCKER_GROUP!r}) — refusing to reap the shared docker host "
        f"for a test that does not drive docker: {offenders[:5]}"
    )
    try:
        with socket.create_connection((_DOCKER_HOST_IP, 22), timeout=2):
            pass
    except OSError:
        return
    with contextlib.suppress(Exception):
        asyncio.run(_reap_orphan_docker_stacks())


def _host_data(ne: str) -> dict[str, Any]:
    for host in flatten_lab_doc(json.loads(_LAB_DATA.read_text())):
        if host["element"] == ne:
            return host
    raise KeyError(f"NE {ne!r} not found in {_LAB_DATA}")


@pytest_asyncio.fixture
async def test1():
    """UnixHost for test1 via SSH."""
    data = _host_data("test1")
    h = UnixHost(
        ip=data["ip"],
        element=data["element"],
        creds=[Cred(**c) for c in data["creds"]],
        board=data.get("board"),
        is_virtual=True,
        term="ssh",
        transfer="scp",
    )
    yield h
    await h.close()


@pytest_asyncio.fixture
async def test2():
    """UnixHost for test2 via SSH."""
    data = _host_data("test2")
    h = UnixHost(
        ip=data["ip"],
        element=data["element"],
        creds=[Cred(**c) for c in data["creds"]],
        board=data.get("board"),
        is_virtual=True,
        term="ssh",
        transfer="scp",
    )
    yield h
    await h.close()


# ---------------------------------------------------------------------------
# The BusyBox bed's family xdist group, re-checked at setup
#
# Two hooks put guest-driving items into one xdist group: the bed suite's own
# conftest stamps everything under ``tests/integration/busybox_bed/``, and
# ``tests/integration/host/conftest.py`` stamps the parametrized rows in the
# generic suites by param value. Both are ``tryfirst`` because pluggy
# dispatches same-phase impls in LIFO registration order and xdist's own
# ``pytest_collection_modifyitems`` is what turns the marker into the nodeid
# suffix ``--dist loadgroup`` schedules on — a stamp applied after xdist ran
# is set, ignored, and silent.
#
# Silent is the problem. When a Zephyr device loses its group, two workers
# drive one console and the guest goes down: loud, and already guarded
# (``_unhonored_group`` in the host conftest, which only ever looks at
# ``embedded`` items). When a BusyBox row loses its group, NOTHING FAILS. The
# five guests are TCG — x86 on an aarch64 host, no KVM — and all five live on
# ``test1``, a two-core VM, so a second worker does not parallelise them: it
# timeshares two cores and takes cycles from guests that are already paying
# for emulation. The run gets slower, more timing-sensitive, and stays green,
# which is how a lost stamp survives three reviews.
# ---------------------------------------------------------------------------


def _touches_the_busybox_bed(item: pytest.Item) -> bool:
    """Whether this item drives one of the five BusyBox guests.

    Read off the NODEID and the bed suite's directory — deliberately NOT off
    ``callspec.params``, which is how the stamping hook in
    ``tests/integration/host/conftest.py`` decides the same question. A guard
    that shares its detector with the mechanism it guards goes quiet in
    exactly the case the detector is wrong: a parametrize shape whose values
    stop matching :data:`~tests.conftest.BUSYBOX_PARAM_TOKENS` (a dataclass
    param, a dict, a deeper nesting) loses the stamp, and a guard asking the
    same question would agree the item was never a bed item and say nothing.
    The nodeid is also the string xdist itself keys the group onto, so it is
    the honest side to read.

    Both spellings are matched as whole id components, because the two row
    shapes render differently: ``host1`` rows read
    ``[busybox_1161-busybox_1161]`` and transfer rows read ``[shell-bb1161]``.
    pytest joins id components with ``-`` and no guest token contains one, so
    splitting on ``-`` recovers the components exactly.

    The residual gap, named rather than papered over: a row that names a guest
    ONLY through a custom ``pytest.param(..., id=...)`` that hides both
    spellings is invisible here. The bed suite is covered by path regardless
    of ids, so the gap is limited to a future generic-suite row that renames
    its own id away from the guest it drives.
    """
    if _BUSYBOX_BED_ROOT in item.path.parents:
        return True
    match = _NODEID_ID.search(item.nodeid)
    return bool(match) and bool(set(match.group(1).split("-")) & BUSYBOX_PARAM_TOKENS)


def _in_the_bed_group(nodeid: str) -> bool:
    """Whether xdist really put this item in the family group.

    The marker proves nothing (see the note above); the ``@group`` suffix
    xdist appends is the evidence, and this reads it. EXACT equality, not
    containment, and the difference is the whole point of the check. xdist
    renders an item carrying several groups as the sorted names joined with
    ``_`` (``xdist/remote.py``: ``f"{nodeid}@{'_'.join(sorted(gnames))}"``),
    and that joined key is a DIFFERENT scheduling unit from ``busybox_bed``:
    ``loadgroup`` keeps each KEY on one worker, not each key that mentions a
    name, so ``busybox_bed_<other>`` is free to run beside the family it reads
    like. Containment would bless precisely the un-serialized case this guard
    exists to catch.

    Safe today, loud tomorrow. Measured hostlessly with ``--setup-plan -n2
    --dist loadgroup`` (2026-08-21): 113 bed-suite items and 61 guest rows in
    the generic suites, and every one of the 174 renders the literal
    ``@busybox_bed`` — no joined key anywhere on the tree — so equality passes
    everything that currently runs. The day a bed row gains a second group this
    fails and forces the decision — one family group, or a deliberate split —
    instead of quietly putting a second worker on the five guests' two TCG
    cores.
    """
    _, sep, suffix = nodeid.rpartition("@")
    return sep == "@" and suffix == BUSYBOX_BED_GROUP


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Fail an item that reaches the BusyBox guests from outside the family group.

    Only under xdist: without it there is one process, nothing to serialize,
    and no suffix to read — the same precondition ``_unhonored_group`` uses.

    Fires at setup, before any fixture runs, so it costs no guest contact even
    when it is right. Deliberately not a collection-time check: at collection
    the group suffix does not exist yet on the controller, and
    ``--collect-only`` never renders one at all — measured twice on this
    branch, it reports a working stamp and a broken stamp identically, so it
    is blind to precisely this defect.

    POSITIVE-CONTROLLED, on both stamping seams, with no bed contact.
    ``--setup-plan`` is the instrument: it runs the collection and setup
    phases (so this hook is dispatched and the ``@group`` suffix is rendered)
    while ``_pytest.fixtures`` fakes every fixture result, so nothing dials a
    guest. Deleting ``tryfirst`` from the bed suite's stamp and running
    ``pytest tests/integration/busybox_bed/ --setup-plan -n2 --dist
    loadgroup`` errors all 113 items here in 0.64s, each report naming the
    worker (``[gw0]``/``[gw1]``) that had picked it up; deleting it from
    ``tests/integration/host/conftest.py`` instead errors the 14
    ``-k "busybox_1161 or bb1161"`` rows of the generic suites the same way.
    With this hook renamed out of pluggy's reach and the same mutation in
    place, both runs are SILENT and every nodeid is suffix-less — which is the
    defect exactly: a lost stamp costs TCG throughput and fails nothing.
    Restoring the decorator restores the suffix on every item.

    No separate unit test, for the reason ``_unhonored_group`` gives: a
    hand-built item would exercise a mock of xdist's behaviour, and xdist's
    behaviour is the whole question.
    """
    if getattr(item.config, "workerinput", None) is None:
        return
    if not _touches_the_busybox_bed(item):
        return
    if _in_the_bed_group(item.nodeid):
        return
    pytest.fail(
        f"this item drives a BusyBox bed guest but xdist did not put it in "
        f"the {BUSYBOX_BED_GROUP!r} group (no '@{BUSYBOX_BED_GROUP}' suffix on "
        f"the nodeid), so a second worker can be on the five guests' two TCG "
        f"cores at the same time. Nothing about that fails on its own — it "
        f"only makes the bed slower and more timing-sensitive — which is why "
        f"it is asserted here.\n"
        f"Three causes, and the fix differs:\n"
        f"  1. A stamping hook stopped running before xdist's. Both "
        f"pytest_collection_modifyitems impls that stamp this group "
        f"(tests/integration/busybox_bed/conftest.py, "
        f"tests/integration/host/conftest.py) are declared `tryfirst` for that "
        f"reason — if a decorator was removed, restore it.\n"
        f"  2. This row's parametrize VALUES no longer match "
        f"BUSYBOX_PARAM_TOKENS (tests/conftest.py), so the host conftest never "
        f"recognised it as a guest row — teach `_names_a_guest` the new shape.\n"
        f"  3. This row's group is deliberately something ELSE. The stamper in "
        f"tests/integration/host/conftest.py does not override an explicit "
        f"`xdist_group` pin, and an `embedded` row that also names a guest keeps "
        f"its Zephyr device group (losing a device group costs the device; "
        f"losing this one costs TCG throughput). A row carrying two groups is "
        f"the same situation rendered differently: xdist joins them into one "
        f"key (`{BUSYBOX_BED_GROUP}_<other>`), which is a DIFFERENT scheduling "
        f"unit and is not serialized with the family. Decide which group owns "
        f"the row — do not let it reach the guests from another one.\n"
        f"Whichever it is, do not silence this check: it is the only thing that "
        f"fails when the grouping stops working.",
        pytrace=False,
    )
