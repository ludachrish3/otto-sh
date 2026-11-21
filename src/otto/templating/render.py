"""The renderer slot: one Jsonnet entry file in, its JSON text out.

:func:`render` is the one call into the binding both consumers make, the
``jsonnet`` lab source and the ``.jsonnet`` inventory. It reads the entry,
passes the top-level arguments as JSON, serves every import through
:func:`~otto.templating.imports.make_callback`, and returns the binding's JSON
text unparsed, with every file the render read. No external variable, native
function or library search path is ever passed.

``std.trace`` output is left alone: both bindings write it to the process's
stderr as Jsonnet prints it, and neither offers a per-evaluation sink, so
otto redirects nothing (an fd-2 redirect is process-wide and would capture
unrelated output).
"""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from typing_extensions import final

from ..utils import resolved_path
from .binding import JsonnetBinding, load_binding
from .errors import RenderCancelled, RenderError
from .imports import REFUSED_PREFIX, ImportPolicy, ImportTrace, make_callback
from .memo import current_cancellation


@final
@dataclass(frozen=True, kw_only=True)
class RenderLimits:
    """The bounds one evaluation runs under, and what each really bounds.

    *max_stack* is enforced by the evaluator: deeper recursion fails the
    render with ``max stack frames exceeded``. *max_output_bytes* is enforced
    by otto on the text the binding returns, before anything parses, copies,
    memoizes or caches it, so otto never accepts or stores an oversized
    document. *max_trace* caps the stack frames an error message lists.

    Neither binding can bound its own memory or be interrupted in-process,
    and otto runs no evaluator in a subprocess: a template that allocates
    without bound, or loops without recursing, runs until it ends. Completion
    bounds its wall time with its existing deadline; a runtime operation has
    no wall-clock limit, like a slow hand-written lab backend.

    The defaults are measured on ``tests/_fixtures/templating/large_fleet.jsonnet``
    (500 labs of 4 role elements each: 2000 elements, 7500 interfaces and
    1500 links, every element built through nested helper functions). Under
    gojsonnet 0.22.0 on an aarch64 Linux VM it needs between 21 and 30 stack
    frames, so ``max_stack`` stays at Jsonnet's own default of 500, and it
    returns 3,154,802 bytes of JSON, so ``max_output_bytes`` is
    ``max(16 MiB, 8 x 3,154,802 B)``, rounded up to 25 MiB. The C++ binding's
    measurement is CI's: ``tests/unit/templating/test_render.py`` renders the
    fleet under both bindings and holds the output to an eighth of the limit.
    """

    max_stack: int = 500
    """The evaluator's stack-frame limit."""
    max_output_bytes: int = 25 * 1024 * 1024
    """The largest rendered document otto accepts, in UTF-8 bytes."""
    max_trace: int = 20
    """The most stack frames an evaluation error lists."""


@final
@dataclass(frozen=True, kw_only=True)
class RenderRequest:
    """One entry file to render, with its top-level arguments and import policy."""

    entry: Path
    """The Jsonnet entry file."""
    tla: "Mapping[str, object] | None"
    """Top-level arguments by name, each passed as JSON; ``None`` passes none."""
    policy: ImportPolicy
    """What the render may import."""
    limits: RenderLimits = RenderLimits()
    """The evaluation's bounds."""


@final
@dataclass(frozen=True, kw_only=True)
class RenderResult:
    """A rendered entry: the binding's JSON text, unparsed, and the files it read."""

    text: str
    """The JSON the evaluation manifested, exactly as the binding returned it."""
    read: tuple[Path, ...]
    """The entry, then each file an import served, in first-read order; every one resolved."""

    def value(self) -> object:
        """Return :attr:`text` decoded: THE one decode of a rendered document, for both consumers.

        The binding manifests JSON, so a decode failure is a guard, not a
        path; it is a :class:`~otto.templating.errors.RenderError` naming
        the entry, which each consumer reports as its render failure.

        Raises:
            RenderError: :attr:`text` is not JSON.
        """
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as e:
            entry = self.read[0]
            raise RenderError(
                f"{entry}: rendered malformed JSON: {e}", origin=entry, line=None
            ) from e


def render(request: RenderRequest) -> RenderResult:
    """Evaluate *request*'s entry once and return its JSON text.

    Raises :class:`~otto.templating.errors.RenderError` naming the file and
    line of the failure (``<origin>:<line>: <message>``, the stack dropped),
    or naming the entry when the binding reported no position; an import the
    policy refuses reports otto's refusal whatever words the binding wraps it
    in. Raises :class:`~otto.templating.errors.RenderCancelled`, without
    entering the evaluator, when the operation's cancellation
    (:func:`~otto.templating.memo.current_cancellation`) is set once the
    inputs are collected.
    """
    entry = request.entry
    resolved_entry = resolved_path(entry)
    if resolved_entry is None:
        raise RenderError(
            f"{entry}: the entry file's path cannot be resolved", origin=entry, line=None
        )
    try:
        source = entry.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise RenderError(f"{entry}: {e}", origin=entry, line=None) from e
    tla_codes = {} if request.tla is None else {k: json.dumps(v) for k, v in request.tla.items()}
    trace = ImportTrace()
    callback = make_callback(request.policy, trace)
    binding = load_binding()
    if current_cancellation():
        raise RenderCancelled(f"{entry}: the operation was cancelled before the render started")
    try:
        text = _evaluate(binding, entry, source, tla_codes, callback, request.limits)
    except RuntimeError as e:
        raise _render_error(str(e), entry=entry, failure=trace.failure) from e
    if trace.failure is not None:  # an importstr or importbin of a refused file
        raise RenderError(f"{entry}: {trace.failure}", origin=entry, line=None)
    size = len(text.encode("utf-8"))
    limit = request.limits.max_output_bytes
    if size > limit:
        raise RenderError(
            f"{entry}: the rendered document is {size} bytes, over the {limit}-byte output limit",
            origin=entry,
            line=None,
        )
    return RenderResult(text=text, read=tuple(dict.fromkeys((resolved_entry, *trace.read))))


def _evaluate(
    binding: JsonnetBinding,
    entry: Path,
    source: str,
    tla_codes: Mapping[str, str],
    import_callback: Callable[[str, str], tuple[str, bytes]],
    limits: RenderLimits,
) -> str:
    """Make the one call into the binding (the seam a test patches to count evaluations)."""
    return binding.evaluate_snippet(
        str(entry),
        source,
        tla_codes=tla_codes,
        import_callback=import_callback,
        max_stack=limits.max_stack,
        max_trace=limits.max_trace,
    )


# The first line of a binding's error. A runtime error is `RUNTIME ERROR: <message>`
# with its position in the frames below; a static (parse) error carries its
# position on this line: `STATIC ERROR: <file>:<pos>: <message>` (C++) or
# `<file>:<pos> <message>` (Go).
_RUNTIME = "RUNTIME ERROR: "
_STATIC = "STATIC ERROR: "
# A Jsonnet position: `<line>:<col>`, `<line>:<col>-<col>` or `(<line>:<col>)-(<line>:<col>)`.
_POSITION = r"\(?(?P<line>\d+):\d+(?:-\d+|\)-\(\d+:\d+\))?"
_HEAD = re.compile(rf"(?P<file>[^\t]+?):{_POSITION}:?\s+(?P<message>.*)")
# A stack frame: a tab, `<file>:<pos>`, a tab and the frame's name.
_FRAME = re.compile(rf"\t(?P<file>[^\t]+?):{_POSITION}(?:\t|$)")


def _render_error(raw: str, *, entry: Path, failure: "str | None") -> RenderError:
    """Turn a binding's error text into ``<origin>:<line>: <message>``, its stack dropped.

    *failure* is the import callback's own message, which replaces the
    binding's. A refused import's stand-in frame is skipped, so the position
    is the import's own.
    """
    lines = raw.splitlines()
    head = lines[0] if lines else ""
    origin, line = entry, None
    if head.startswith(_RUNTIME):
        message = head[len(_RUNTIME) :]
        for frame in lines[1:]:
            found = _FRAME.match(frame)
            if found and not found["file"].startswith(REFUSED_PREFIX):
                origin, line = Path(found["file"]), int(found["line"])
                break
    else:
        head = head.removeprefix(_STATIC)
        found = _HEAD.fullmatch(head)
        if found:
            origin, line, message = Path(found["file"]), int(found["line"]), found["message"]
        else:
            message = head
    message = failure or message.strip() or "the Jsonnet evaluator failed without a message"
    where = f"{origin}:{line}" if line is not None else f"{origin}"
    return RenderError(f"{where}: {message}", origin=origin, line=line)
