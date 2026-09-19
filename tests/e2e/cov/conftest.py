"""Coverage e2e tree conftest: the kgcov observation hook.

One wrapper over the report hook, the way ``tests/conformance/conftest.py``
wires its own: the outcome is read off pytest's report after it exists.
Every other item in this tree — the routine coverage e2es — measures no
matrix column and writes nothing (``profile_of_item`` answers None).

Not guarded by ``suppress``. An emitter that failed quietly would leave a
run looking measured while it recorded nothing — worse than the loud
INTERNALERROR a raise here produces, and this hook does raise: a placed
item whose venue disagrees with its own row in the surface table, or a
write attempted from an xdist worker, are both hook faults and both surface
rather than swallow.
"""

import pytest

from tests.e2e.cov._kgcov_observation import observations_dir, record_phase


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    record_phase(item, report, observations_dir())
    return report
