"""The record of where otto's behaviour on one host, frame or route falls short.

otto's behaviour promises (a command's ``value`` is its exact output, a
command line of any length runs whole, and so on) hold on every host, frame
and route unless the device makes them impossible. A difference is never
left implicit: it is declared, in data, as a :class:`Limitation` next to the
code it applies to, with the reason it cannot be helped and what it was
measured on.

Two kinds are allowed, and which one a declaration may use depends on who
implements the code:

* ``"device"`` -- the device cannot do it. This is the ONLY kind code otto
  implements itself may declare: anything else otto could fix, so it is a
  bug, not a limitation.
* ``"library"`` -- an underlying library (asyncssh, docker) behaves this way
  intrinsically, and otto cannot change it without replacing the library.

The places that declare are :attr:`otto.host.command_frame.CommandFrame.exact_output`,
:data:`otto.host.session.EXEC_ROUTES`, and a console server's own facts in
:class:`otto.host.options.ConsoleOptions` (:attr:`~otto.host.options.ConsoleOptions.exact_output`).

This module is a leaf: it imports nothing from otto, so any module may
declare a limitation without creating an import cycle.
"""

from dataclasses import dataclass

__all__ = ["Limitation"]

_KINDS = ["device", "library"]
"""Every value :attr:`Limitation.kind` may take."""


@dataclass(frozen=True, kw_only=True)
class Limitation:
    """One declared difference from a behaviour promise, with its evidence.

    Checked at construction: a declaration without a reason or a measurement
    is a broken import, not a record that quietly says nothing.

    Raises:
        ValueError: naming the field that is empty or not allowed.
    """

    kind: str
    """``"device"`` (the device cannot do it) or ``"library"`` (a library otto
    builds on cannot, intrinsically)."""

    affects: str
    """The promise this qualifies, in words a user reads ("how long a command
    line can be"). Never empty."""

    reason: str
    """Why the promise cannot hold here, in one plain sentence a user can act
    on. Never empty."""

    measured_on: list[str]
    """The real measurements behind the difference, one short factual entry
    per item (date, device and version, result). Never empty, and never
    "not measured" -- a difference nobody measured is not a limitation."""

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"Limitation.kind must be one of {_KINDS}, got {self.kind!r}")
        if not self.affects.strip():
            raise ValueError("Limitation.affects must name the promise it qualifies")
        if not self.reason.strip():
            raise ValueError("Limitation.reason must say why the promise cannot hold")
        if not self.measured_on or not all(item.strip() for item in self.measured_on):
            raise ValueError(
                "Limitation.measured_on must name at least one measurement, none empty"
            )
