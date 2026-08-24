"""The shared bootstrap stub must declare every field the real result has.

Without this, a new ``BootstrapResult`` field is discovered the way the last one
was: as an ``AttributeError`` in ten preamble tests about reservations, output
dirs and dispatch. Here it is one red cell that names the field.
"""

import dataclasses

from otto.bootstrap import BootstrapResult
from tests._fixtures.bootstrapstub import bootstrap_stub


def test_the_stub_declares_every_field_the_real_result_has() -> None:
    missing = {f.name for f in dataclasses.fields(BootstrapResult)} - set(vars(bootstrap_stub()))
    assert not missing, (
        f"tests/_fixtures/bootstrapstub.py is missing {sorted(missing)} — add them there "
        f"rather than in whichever preamble test noticed first"
    )
