"""The failures Jsonnet templating raises.

A template that cannot be rendered is otto's own concept, never a stdlib error a
caller was already catching, so these classes keep no stdlib root
(``tests/unit/test_error_base.py`` lists them as rootless on purpose).
"""

from pathlib import Path

from ..errors import OttoError


class TemplatingError(OttoError):
    """A Jsonnet template could not be used: no binding, or a render that failed."""


class TemplatingUnavailableError(TemplatingError):
    """No Jsonnet binding imports on this platform, so no template can be rendered.

    otto installs one binding per platform by marker (``pyproject.toml``); on a
    platform with none, otto still installs and every template fails with this
    error, whose message names the platform and the Python.
    """


class RenderError(TemplatingError):
    """A template failed to render: an error in the Jsonnet, or an import it may not read.

    *origin* is the file the failure is reported against and *line* its line,
    or ``None`` when the binding named no position.
    """

    def __init__(self, message: str, *, origin: Path, line: int | None) -> None:
        super().__init__(message)
        self.origin = origin
        """The file the failure is reported against."""
        self.line = line
        """The failure's line in *origin*, or ``None`` when no position was named."""


def without_entry(error: object, entry: Path) -> str:
    """Return the render failure *error* as text, without the ``<entry>:`` that leads it.

    The one rule for naming a failed entry once: a render's failure names
    its entry, because some readers print it alone, and a consumer whose own
    message already leads with the file (a lab file, an inventory file)
    states the rest. A location (``<entry>:<line>: …``) is kept whole: it
    carries the line.
    """
    return str(error or "").removeprefix(f"{entry}: ")


class RenderCancelled(TemplatingError):  # noqa: N818 — interface-fixed name: a cancellation, not a fault
    """The operation a render belonged to was cancelled before the evaluator was entered.

    Raised by :func:`~otto.templating.render.render` when the operation's
    cancellation (:func:`~otto.templating.memo.current_cancellation`) is set;
    :class:`~otto.templating.memo.RenderMemo` settles it as the render's
    timed-out outcome, never as an error.
    """
