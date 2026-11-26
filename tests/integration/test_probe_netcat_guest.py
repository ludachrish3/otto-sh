"""The probe's netcat listener on the Ubuntu 24.04 guest, once per netcat implementation it carries.

``otto host <id> probe`` measures the listener checks against one netcat
listener it opens, counts and closes. The guest behind ``test4`` carries
several netcat implementations, each selected by its ``nc_options.exec_name``,
and ``tests/conformance/_nc_rungs.py`` records which listener checks work with
each. These tests hold the survey's result to that record, check the guest's
listeners are the same before and after a survey, and check that a survey
cancelled while its listener is open leaves no netcat listener behind.

The guest is opened by the bed's netcat opener (``nc_bed_cell``), which
resolves its hop through ``test4``. Every test here is ``serial_timing``, so
a lane runs them one after another with no other test beside them: no other
test opens a listener on the guest between a test's two reads, and none takes
CPU from the emulated guest while the product's elapsed-time bounds run. The
xdist group is for a test added here without that mark: it would stay on one
worker with its neighbours.
"""

import pytest

from otto.host.survey import run_survey
from tests.conformance._bed import NETCAT_ONLY_ELEMENTS, nc_bed_cell
from tests.conformance._nc_rungs import (
    BED_NETCAT,
    DOES_NOT_WORK,
    LADDERS,
    LISTENER,
    NC_IMPLEMENTATION_CELLS,
    implementation,
    nc_implementation_id,
    working_checks,
)
from tests.conftest import NETCAT_GUEST_GROUP
from tests.integration._probe_listener import (
    assert_an_interrupted_survey_leaves_no_listener,
    listeners_of,
)

pytestmark = [pytest.mark.timeout(150), pytest.mark.xdist_group(NETCAT_GUEST_GROUP)]

_GUEST_IMPLEMENTATIONS = [
    cell for cell in NC_IMPLEMENTATION_CELLS if cell[0] in NETCAT_ONLY_ELEMENTS
]
"""One ``(element, exec_name)`` per netcat implementation on the guest."""

_GUEST_FIRST_IMPLEMENTATION = [
    (element, BED_NETCAT[element].implementations[0].exec_name) for element in NETCAT_ONLY_ELEMENTS
]
"""The guest's first recorded implementation, the one the interrupted probe runs with."""


def _selecting(exec_name: str) -> dict[str, dict[str, object]]:
    """The option override that selects the netcat implementation *exec_name*."""
    return {"nc_options": {"exec_name": exec_name}}


async def _warm(host) -> None:
    # The guest's first login after it was idle is slow under emulation and
    # exceeds the survey's login bound. One command first makes the survey's
    # login an ordinary one.
    await host.exec("true")


@pytest.mark.serial_timing
@pytest.mark.asyncio
@pytest.mark.parametrize("nc_impl", _GUEST_IMPLEMENTATIONS, ids=nc_implementation_id)
async def test_the_probe_measures_the_listener_ladder_with_each_netcat(nc_impl):
    """The survey measures the listener check the record says works first, and leaves no listener.

    The measured check is the first in ladder order that the record lists as
    working with this implementation, and every check before it that the
    record lists as not working is named in the survey's failed strategies.
    The guest's listeners, read with the survey's own inventory, are the same
    before and after, and the survey reports that it confirmed its listener
    ended.

    It runs alone (``serial_timing``): the survey and the two reads carry the
    product's own elapsed-time bounds, five seconds for an inventory read
    among them. Tests on other workers that go through ``test4`` take CPU from
    the emulated guest that VM hosts, and a read on an open session then passes
    five seconds, against 0.4 seconds with nothing beside it.
    """
    element, exec_name = nc_impl
    label = nc_implementation_id(nc_impl)
    impl = implementation(element, exec_name)
    expected = working_checks(impl)[0]
    order = LADDERS[LISTENER]
    not_working_before = [
        rung
        for rung in order[: order.index(expected)]
        if impl.listener[rung].state == DOES_NOT_WORK
    ]
    async with nc_bed_cell(element).open_host_with(_selecting(exec_name)) as host:
        await _warm(host)
        before = await listeners_of(host, label)
        s = await run_survey(host)
        after = await listeners_of(host, label)
    assert s.nc_rungs is not None, label
    assert (s.nc_rungs.listener.state, s.nc_rungs.listener.rung) == ("measured", expected), (
        label,
        s.nc_rungs,
    )
    failed = [f.strategy for f in s.nc_rungs.failed if f.ladder == LISTENER]
    assert set(not_working_before) <= set(failed), (label, s.nc_rungs)
    assert s.nc_rungs.listener_left == "", (label, s.nc_rungs)
    assert before, label
    assert after == before, label


@pytest.mark.serial_timing
@pytest.mark.asyncio
@pytest.mark.parametrize("nc_impl", _GUEST_FIRST_IMPLEMENTATION, ids=nc_implementation_id)
async def test_an_interrupted_probe_leaves_no_listener(nc_impl):
    """A probe cancelled while its netcat listener is open leaves no netcat listener on the guest.

    The survey is held once its listener has been counted, the guest is seen
    to list its netcat listener, and the survey is then cancelled. It returns
    within the backend's reap bound, and the guest then lists no netcat
    listener within a few seconds. The observations run on a second host
    object for the same guest.

    It runs alone (``serial_timing``): its assertions reject a slow teardown by
    elapsed time, which load from other workers lengthens on an emulated guest.
    """
    element, exec_name = nc_impl
    label = nc_implementation_id(nc_impl)
    bed_cell = nc_bed_cell(element)
    async with (
        bed_cell.open_host_with(_selecting(exec_name)) as host,
        bed_cell.open_host_with(_selecting(exec_name)) as observer,
    ):
        await _warm(host)
        await assert_an_interrupted_survey_leaves_no_listener(host, observer, label)
