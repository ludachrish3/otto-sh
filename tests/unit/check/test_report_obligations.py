"""``ReportVerdicts``'s two readers are obligations the two report dataclasses satisfy.

``refusal`` is an abstract property on the base and a dataclass field with a
default on each report: the default puts ``refusal = None`` in the class
namespace, which is what overrides the abstract property. A field without a
default (or with ``default_factory``) would leave it abstract, so the pin is
on the instantiable classes, not on the base.
"""

import inspect

import pytest

from otto.check.verdict import ReportVerdicts
from otto.link.check import LinkCheckReport
from otto.tunnel.check import TunnelCheckReport

pytestmark = pytest.mark.interpreter_agnostic


def test_the_base_declares_its_two_readers():
    assert ReportVerdicts.__abstractmethods__ == frozenset({"refusal", "results"})
    assert inspect.isabstract(ReportVerdicts)


@pytest.mark.parametrize("report", [LinkCheckReport, TunnelCheckReport])
def test_each_report_satisfies_both(report):
    assert report.__abstractmethods__ == frozenset()
