"""Pydantic boundary models for the monitor subsystem.

Two seams:

* :class:`MetricPoint` — the in-memory series element (replaces the old
  ``(ts, value, meta)`` 3-tuple in ``MetricCollector._series``). It is an
  :class:`~otto.models.base.OttoModel` (``extra='forbid'``) because otto is the
  only thing that builds it: the live append path uses ``model_construct`` (no
  validation, hot loop) and the import path uses ``model_validate``.

* :class:`MetricRecord` / :class:`EventRecord` — flat records at the JSON
  ``--file`` and SQLite import/export boundary. These read *historical,
  external* data, so they are deliberately **lenient** (``extra='ignore'``,
  via :class:`_RowModel`): an unknown column from a newer schema is dropped, not
  rejected, exactly as the old ``.get()``/``[]`` parsing did. Field names follow
  the JSON spelling; a ``validation_alias`` also accepts the SQLite column
  spelling (``ts``/``end_ts``) so one model validates both seams.

Leaf isolation: this module imports only :mod:`otto.models.base`, pydantic, and
the stdlib — no runtime or ``otto.monitor`` edge — so it stays a pure leaf inside
the models package.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from .base import OttoModel


class MetricPoint(OttoModel):
    """A single charted sample: timestamp, numeric value, optional hover meta.

    Replaces the ``(datetime, float, dict | None)`` tuple stored per series.
    Consumers read ``.ts`` / ``.value`` / ``.meta`` instead of unpacking.
    """

    ts:    datetime
    value: float
    meta:  dict[str, Any] | None = None


class _RowModel(BaseModel):
    """Lenient base for historical-data import/export rows.

    Unlike :class:`~otto.models.base.OttoModel` (``extra='forbid'``, which exists
    to turn a *config* typo into an error), data read-back is tolerant: an
    unexpected key/column from a newer schema is ignored rather than rejected.
    This matches the pre-pydantic ``.get()``/``[]`` parsing and keeps older otto
    builds able to import exports written by newer ones.
    """

    model_config = ConfigDict(extra="ignore")


class MetricRecord(_RowModel):
    """One ``metrics`` row at the JSON / SQLite import-export boundary.

    The JSON ``--file`` format spells the time key ``timestamp``; the SQLite
    ``metrics`` table column is ``ts``. The ``validation_alias`` accepts both, so
    a single model validates either seam. ``host`` is optional for the
    pre-host-column schema; ``meta`` rides only in JSON (the DB has no meta
    column). Exporting with ``model_dump(mode='json', exclude_none=True)`` emits
    the JSON spelling and omits ``meta`` when ``None`` (``host=''`` is still
    emitted — empty string is not ``None``).
    """

    timestamp: datetime = Field(validation_alias=AliasChoices("timestamp", "ts"))
    host:      str = ""
    label:     str
    value:     float
    meta:      dict[str, Any] | None = None


class EventRecord(_RowModel):
    """One ``events`` row at the JSON / SQLite **import** boundary.

    Mirrors the ``MonitorEvent`` fields. Used to validate external event data
    before constructing the (unchanged, mutable) ``MonitorEvent`` dataclass —
    event *export* stays ``MonitorEvent.to_dict()``. ``timestamp`` is required
    (a row without one is skipped, as before); everything else defaults. ``id``
    is ``None`` when absent so the collector can assign its running id.
    """

    id:            int | None = None
    timestamp:     datetime = Field(validation_alias=AliasChoices("timestamp", "ts"))
    end_timestamp: datetime | None = Field(
        default=None, validation_alias=AliasChoices("end_timestamp", "end_ts")
    )
    label:         str = ""
    source:        str = "manual"
    color:         str = "#888888"
    dash:          str = "dash"
