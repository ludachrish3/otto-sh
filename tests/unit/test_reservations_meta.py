"""Tests for gate() reading reservation state from ctx.meta."""

import types

from otto.reservations.check import ReservationState, gate


def _fake_ctx(meta: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(meta=meta)


def test_gate_noops_with_empty_meta():
    gate(_fake_ctx({}))  # no reservation configured -> no exception, no get_lab needed


def test_gate_noops_when_backend_none():
    gate(
        _fake_ctx(
            {"otto_reservation": ReservationState(backend=None, identity=None, skip_check=False)}
        )
    )
