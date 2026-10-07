"""The JSON reservation file against frozen samples (dump spec §13.5)."""

import json
from datetime import datetime, timezone

import pytest

from otto.models import formats
from otto.reservations.check import ReservationBackendError
from otto.reservations.json_backend import JsonReservationBackend
from tests._fixtures.paths import TESTS_ROOT

SAMPLES = TESTS_ROOT / "_fixtures" / "formats" / "reservations-file"
UTC = timezone.utc


def _v1(backend: JsonReservationBackend) -> None:
    assert sorted((r.resource, r.end) for r in backend.fetch_reservations("alice")) == [
        ("rack3-console", datetime(2099, 1, 1, tzinfo=UTC)),
        ("rack3-psu", datetime(2099, 1, 1, tzinfo=UTC)),
        ("rack5-psu", datetime(2099, 6, 1, 10, tzinfo=UTC)),
    ]
    assert [(r.user, r.end) for r in backend.holders("rack4-psu")] == [("bob", None)]
    # carol's entry on the same resource expired in 2001, so only alice holds it
    assert [r.user for r in backend.holders("rack3-psu")] == ["alice"]


MEANING = {1: _v1}
"""What each declared read version's sample must read as; a version with no entry fails."""


@pytest.mark.parametrize("version", formats.RESERVATIONS_READ_VERSIONS)
def test_the_json_backend_reads_each_declared_sample(version):
    MEANING[version](JsonReservationBackend(path=SAMPLES / f"{version}.json", username="alice"))


def test_the_json_backend_refuses_a_version_outside_the_declared_reads(tmp_path):
    """The field's ``Literal`` is the declared list, so the next version up is refused."""
    path = tmp_path / "reservations.json"
    path.write_text(
        json.dumps({"version": max(formats.RESERVATIONS_READ_VERSIONS) + 1, "reservations": []})
    )
    backend = JsonReservationBackend(path=path, username="alice")
    with pytest.raises(
        ReservationBackendError, match=r"version\n\s+Input should be .*\[type=literal_error"
    ):
        backend.fetch_reservations("alice")
