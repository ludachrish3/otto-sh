"""One way to declare a suite's options: the ``Options`` class attribute (spec §7).

``OttoSuite[_Opts]`` used to be accepted alongside ``Options = _Opts``; otto only
ever read the attribute, and the generic parameter bound a TypeVar nothing used.
The subscript form is now a loud TypeError at the definition site.
"""

from typing import Annotated

import pytest
import typer

from otto import options
from otto.suite.register import SUITES
from otto.suite.suite import OttoSuite


@options
class _Opts:
    retries: Annotated[int, typer.Option(help="n")] = 3


def test_subscripting_ottosuite_is_a_type_error() -> None:
    """``class TestX(OttoSuite[_Opts])`` fails where it is written, not later."""
    with pytest.raises(TypeError, match="not subscriptable"):
        OttoSuite[_Opts]


def test_base_class_declares_no_options() -> None:
    assert OttoSuite.Options is None


def test_options_attribute_is_what_registration_reads() -> None:
    """A bare subclass with ``Options = _Opts`` registers with the attribute's flags."""

    class TestDeclProbe(OttoSuite):
        Options = _Opts

        async def test_nothing(self) -> None:
            pass

    entry = SUITES.get("TestDeclProbe")
    assert entry.cls is TestDeclProbe
    assert entry.cls.Options is _Opts
    callback = entry.sub_app.registered_commands[0].callback
    param_names = {p.name for p in callback.__signature__.parameters.values()}  # type: ignore[union-attr]
    assert "retries" in param_names
