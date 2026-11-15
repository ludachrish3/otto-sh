"""Wire ids and counts are JSON integers only (#1035).

An event id, a deleted event's id and a source's ``unsaved``/``revision``
counts load only from an integer: never a bool (``true`` would become id 1), a
float (``3.0``) or a numeric string. Every model that carries one is driven
through the same inputs, from JSON and from Python.
"""

import json
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from otto.models.monitor import (
    EventRecord,
    MonitorSessionFragment,
    MonitorSourceState,
    SourceSavedBody,
)

T0 = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc).isoformat()

_STATE: dict[str, Any] = {
    "mode": "review",
    "kind": "json",
    "source": "a.json",
    "token": "t",
    "editable": True,
    "unsaved": 0,
    "revision": 0,
    "observed_at": T0,
}


def _event(value: object) -> dict[str, Any]:
    return {"id": value, "timestamp": T0}


def _fragment(value: object) -> dict[str, Any]:
    return {"session": "s", "deleted_event_ids": [value]}


def _unsaved(value: object) -> dict[str, Any]:
    return {**_STATE, "unsaved": value}


def _revision(value: object) -> dict[str, Any]:
    return {**_STATE, "revision": value}


def _saved(value: object) -> dict[str, Any]:
    return {"token": "t", "revision": value}


COUNT_SITES = [
    pytest.param(MonitorSourceState, _unsaved, lambda m: m.unsaved, id="unsaved"),
    pytest.param(MonitorSourceState, _revision, lambda m: m.revision, id="revision"),
    pytest.param(SourceSavedBody, _saved, lambda m: m.revision, id="saved-revision"),
]

SITES = [
    pytest.param(EventRecord, _event, lambda m: m.id, id="event-id"),
    pytest.param(
        MonitorSessionFragment, _fragment, lambda m: m.deleted_event_ids[0], id="deleted-event-id"
    ),
    *COUNT_SITES,
]

NOT_AN_INTEGER = "Input should be a valid integer"

NOT_INTEGERS = [
    pytest.param(True, id="true"),
    pytest.param(False, id="false"),
    pytest.param(3.0, id="float"),
    pytest.param("3", id="string"),
]


@pytest.mark.parametrize(("model", "body", "field"), SITES)
@pytest.mark.parametrize("value", NOT_INTEGERS)
def test_a_non_integer_is_refused(
    model: type[BaseModel], body: Any, field: Any, value: object
) -> None:
    with pytest.raises(ValidationError, match=NOT_AN_INTEGER):
        model.model_validate_json(json.dumps(body(value)))
    with pytest.raises(ValidationError, match=NOT_AN_INTEGER):
        model.model_validate(body(value))


@pytest.mark.parametrize(("model", "body", "field"), SITES)
def test_an_integer_loads(model: type[BaseModel], body: Any, field: Any) -> None:
    for loaded in (model.model_validate_json(json.dumps(body(3))), model.model_validate(body(3))):
        value = field(loaded)
        assert (value, type(value)) == (3, int)


@pytest.mark.parametrize(("model", "body", "field"), COUNT_SITES)
def test_a_negative_count_is_refused(model: type[BaseModel], body: Any, field: Any) -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        model.model_validate_json(json.dumps(body(-1)))
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        model.model_validate(body(-1))


@pytest.mark.parametrize("key", ["unsaved", "revision"])
def test_a_source_state_always_carries_its_counts(key: str) -> None:
    """The server always sends both counts, so a state without one is refused."""
    state = {k: v for k, v in _STATE.items() if k != key}
    with pytest.raises(ValidationError, match="Field required"):
        MonitorSourceState.model_validate(state)
