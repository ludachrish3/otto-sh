"""Negative typing fixture: to_host takes the readiness record, by keyword."""

from otto.host import UnixHostSpec


def build(spec: UnixHostSpec) -> None:
    spec.to_host()  # ty: ignore[missing-argument]
