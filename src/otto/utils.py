"""Shared helpers that command modules and the runtime both import.

Among others: the :class:`Status` enum, the CLI overlays (:class:`Arg`,
:class:`Opt`, ``Exclude``) and :func:`cli_exposed`, path anchoring
(:func:`anchor_path`), the ``cache_ttl`` grammar (:func:`parse_cache_ttl`), the
monitor collection-interval floor (:data:`MIN_INTERVAL_SECONDS`), list-option
splitting and completion helpers, the dry-run headlines, and deadline polling
(:func:`wait_for`, :func:`wait_for_async`).
"""

import inspect
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Literal,
    TypeVar,
    Union,
    get_args,
    get_origin,
    overload,
)

from .errors import OttoError

__all__ = ["Arg", "Exclude", "Opt", "Status", "cli_exposed"]


def anchor_path(value: Path, root: Path) -> Path:
    """Expand ``~``, then anchor a still-relative path to *root*.

    ``settings.toml`` is committed and shared team-wide, so a CWD-relative
    value in it can never resolve stably. Absolute paths (including
    ``~``-rooted ones, already expanded here) pass through untouched.

    Deliberately does not ``resolve()``: that would collapse symlinks and
    change path identity for repos reached through symlinked checkouts.

    Raises:
        ValueError: *value*'s ``~`` cannot be expanded (``~nosuchuser/...``,
            or no home directory at all). ``expanduser`` raises ``RuntimeError``
            there; a ``ValueError`` is what a pydantic validator turns into a
            settings error naming the field, and what tolerant readers catch.
    """
    try:
        value = value.expanduser()
    except RuntimeError as e:
        raise ValueError(f"cannot expand {value}: {e}") from e
    return value if value.is_absolute() else root / value


_TTL = re.compile(r"\A(?:0|([1-9]\d*)([mhd]))\Z")
"""``\\Z``, not ``$``: ``$`` also matches before a trailing newline, so ``"24h\\n"``
would parse — and a settings value with a stray newline must be refused, not guessed at."""

_TTL_UNIT = {"m": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1)}


def parse_cache_ttl(text: str) -> timedelta:
    """``"24h"`` → 24 hours; ``"0"`` → no caching (spec §9.5). Units: ``m``, ``h``, ``d``.

    Lives here, beside :func:`anchor_path` and for the same reason, rather than
    in the inventory package or beside the inventory record:
    :class:`~otto.models.settings.InventoryConfigSpec` validates ``cache_ttl``
    at the settings boundary on every command, a boundary model may not import
    a runtime package, and :mod:`otto.models.inventory` is built from the host
    specs, which would put the host classes on every command that reads
    settings. Deliberately narrow: no leading zeros, no whitespace, no
    fractions, no week/second units — one spelling per duration, so two
    settings files that mean the same thing look the same.

    Every rejection is a ``ValueError``, including an out-of-range one. The
    grammar admits arbitrarily many digits but ``timedelta`` does not, and an
    ``OverflowError`` escaping here would sail through every caller's
    ``except ValueError`` (the pydantic validator, ``load_user_settings``,
    ``build_inventory``) and reach the user as a bare traceback naming no file.
    """
    m = _TTL.match(text)
    if m is None:
        raise ValueError(f"cache_ttl must be '0' or <n>m / <n>h / <n>d, got {text!r}")
    if m.group(1) is None:
        return timedelta(0)
    try:
        return int(m.group(1)) * _TTL_UNIT[m.group(2)]
    except OverflowError as e:
        raise ValueError(f"cache_ttl {text!r} is out of range: {e}") from e


MIN_INTERVAL_SECONDS: float = 1.0
"""The monitor collection-interval floor — one home for the constant and the check.

An interval below one second is not meaningful in practice: a host must be
given time to answer every query in the interval without being taxed by the
polling itself. The floor is enforced where a *human* names an interval — the
CLI, the library, the pytest plugin — and NOT in
:class:`~otto.monitor.collector.MetricCollector`, which is the mechanism
rather than a knob: the monitor tests drive it at 0.01-0.2s against fake
hosts, where no real host is ever polled.
"""


def validate_interval(seconds: float) -> float:
    """Return *seconds*, or raise ``ValueError`` if it is below the floor.

    The one home for the refusal's wording. It names no field and no flag,
    and avoids the bare word "monitor", which a CLI's field-to-flag spelling
    would rewrite as ``--monitor``: each caller wraps the text in its own
    field-named error.
    """
    if seconds < MIN_INTERVAL_SECONDS:
        raise ValueError(
            f"interval must be at least {MIN_INTERVAL_SECONDS}s, got {seconds}s — "
            "a host needs time to answer every query in the interval without being "
            "taxed by the polling itself."
        )
    return seconds


def compile_host_pattern(text: str) -> "re.Pattern[str]":
    """Compile a host-id selection regex, or raise ``ValueError`` naming it.

    The message names no field or flag: each caller wraps the ``ValueError``
    in its own field-named error.
    """
    try:
        return re.compile(text)
    except re.error as exc:
        raise ValueError(f"host pattern {text!r} is not a valid regex: {exc}") from exc


def split_on(values: list[str] | str, sep: str = ",") -> list[str]:
    """Split a string or list of strings on *sep* into a flat list.

    Args:
        values: A single *sep*-separated string, or a list of such strings.
        sep: The separator to split on.

    Returns:
        A flat list of the individual values.

    >>> split_on("a,b,c")
    ['a', 'b', 'c']
    >>> split_on(["a,b", "c,d"])
    ['a', 'b', 'c', 'd']
    >>> split_on("a+b", sep="+")
    ['a', 'b']
    >>> split_on("single")
    ['single']
    """
    all_values: list[str] = []

    match values:
        case str():
            return values.split(sep)

        case list():
            for value in values:
                all_values += split_on(value, sep)

            return all_values


def complete_separated_list(candidates: list[str], incomplete: str, sep: str = ",") -> list[str]:
    """Filter *candidates* for tab-completing one entry of a *sep*-separated option.

    Options like ``--lab a+b`` and ``--hosts x,y`` take a separator-joined value,
    which the shell hands to the completer as a single ``incomplete`` word.
    Complete only the final (in-progress) segment, keep the already-typed prefix
    intact, and drop candidates already present earlier in the list so completion
    never re-offers them.

    >>> complete_separated_list(["tech1", "tech2", "prod"], "tech")
    ['tech1', 'tech2']
    >>> complete_separated_list(["tech1", "tech2", "prod"], "tech1,te")
    ['tech1,tech2']
    >>> complete_separated_list(["tech1", "tech2", "prod"], "tech1+te", sep="+")
    ['tech1+tech2']
    """
    head, found, frag = incomplete.rpartition(sep)
    already = set(head.split(sep)) if found else set()
    prefix = head + found  # "" when the separator has not been typed yet
    return [prefix + c for c in candidates if c.startswith(frag) and c not in already]


_MARKER_EXPRESSION_KEYWORDS = frozenset({"and", "or", "not"})


def complete_marker_expression(candidates: list[str], incomplete: str) -> list[str]:
    """Complete the trailing identifier of a pytest ``-m`` expression; keep the head verbatim.

    ``smoke and not (sl`` → the head is everything up to and including the
    last whitespace or ``(`` (``smoke and not (``), the tail ``sl`` is what is
    being typed. Candidates are the marker names starting with the tail, each
    emitted as head plus name, in the order given; the expression keywords
    are never offered. Mirrored byte-for-byte by the completion shim
    (``otto._shim_complete``), so change both or neither.

    >>> complete_marker_expression(["slow", "smoke"], "smoke and s")
    ['smoke and slow', 'smoke and smoke']
    """
    cut = max(incomplete.rfind(" "), incomplete.rfind("\t"), incomplete.rfind("("))
    head, tail = incomplete[: cut + 1], incomplete[cut + 1 :]
    return [
        head + c for c in candidates if c.startswith(tail) and c not in _MARKER_EXPRESSION_KEYWORDS
    ]


class WaitTimeoutError(OttoError, TimeoutError):
    """Raised by :func:`wait_for` / :func:`wait_for_async` when the budget expires.

    A dedicated subclass so callers can tell the helper's own expiry apart
    from a ``TimeoutError`` raised *by the predicate* (which propagates
    unchanged) — a bare ``except TimeoutError`` around a wait would conflate
    the two. OttoError-rooted per the repo-wide raises convention
    (``tests/unit/test_error_base.py``), keeping ``TimeoutError`` as its
    stdlib root so both spellings catch it.
    """


def _render_on_timeout(on_timeout: str | Callable[[], str]) -> str:
    return on_timeout if isinstance(on_timeout, str) else on_timeout()


def _check_timeout(timeout: float) -> None:
    # NaN poisons both the expiry comparison and the sleep cap into silently
    # never firing — the same reason host.py's _validate_timeout rejects it.
    if math.isnan(timeout):
        raise ValueError("wait_for timeout must not be NaN")


def _interval_at(interval: float | Callable[[int], float], sleep_index: int) -> float:
    value = interval if isinstance(interval, int | float) else interval(sleep_index)
    # Rejects negatives and NaN in one comparison (asyncio.sleep would
    # silently treat a negative as 0 where time.sleep raises). Zero stays
    # legal: sleep(0) is a yield, the deliberate spelling of a tight poll
    # against an in-process condition (mock-backed tests rely on it).
    if not value >= 0:
        raise ValueError(
            f"wait_for interval must be non-negative, got {value!r} (sleep {sleep_index})"
        )
    return value


def wait_for(
    predicate: Callable[[], bool],
    timeout: float,
    *,
    interval: float | Callable[[int], float] = 0.1,
    probe_first: bool = True,
    on_timeout: str | Callable[[], str],
) -> None:
    """Poll *predicate* until it returns true, or raise :class:`WaitTimeoutError` at *timeout*.

    The one sanctioned spelling of poll-until-deadline (gate G6): expiry always
    raises :class:`WaitTimeoutError` (a ``TimeoutError`` subclass, so it stays
    distinguishable from a timeout raised by the predicate itself) with the
    rendered *on_timeout* message — there is no
    return-``False`` mode, because silent expiry is the defect class this
    replaces. Callers that genuinely want a boolean wrap the call in
    ``try/except WaitTimeoutError``.

    ``probe_first=True`` probes once *before* consulting the clock, so an
    already-exhausted budget still gets exactly one probe: a caller whose
    earlier phase consumed the whole budget landing right at the edge must not
    fail unprobed — one clean success is a success no matter what the clock
    says. ``probe_first=False`` sleeps one *interval* first, for conditions
    that cannot possibly hold at t=0 (e.g. waiting out a scheduled expiry).

    *interval* may be a callable mapping the 0-based index of the upcoming
    sleep to its duration, for ramped polls (probe fast while the condition
    usually turns true, back off after). The final sleep is capped to the
    remaining budget, with one last probe at the deadline edge — the total
    wall time never overshoots *timeout* by a full interval.

    *on_timeout* may be a callable rendering the message lazily, so the
    failure text can name the last-observed state; it is only invoked on
    expiry. Exceptions raised by *predicate* propagate unchanged — a probe
    that can detect "this can never succeed" (a dead child process, say)
    should raise, not return ``False``. A predicate that must hand a value
    back (the matched text, an opened connection) sets it via a
    closure-captured variable before returning ``True``.
    """

    def probe() -> bool:
        result = predicate()
        if inspect.isawaitable(result):
            # A coroutine object is truthy, so an async predicate handed to
            # the sync twin would report instant success — the silent-success
            # defect class this helper exists to kill. Close it (silencing
            # the never-awaited warning) and refuse loudly.
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError("wait_for predicate returned an awaitable — use wait_for_async")
        return result

    _check_timeout(timeout)
    deadline = time.monotonic() + timeout
    if probe_first and probe():
        return
    sleeps = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WaitTimeoutError(_render_on_timeout(on_timeout))
        time.sleep(min(_interval_at(interval, sleeps), remaining))
        sleeps += 1
        if probe():
            return


async def wait_for_async(
    predicate: Callable[[], bool | Awaitable[bool]],
    timeout: float,
    *,
    interval: float | Callable[[int], float] = 0.1,
    probe_first: bool = True,
    on_timeout: str | Callable[[], str],
) -> None:
    """Async twin of :func:`wait_for`; awaits *predicate* if it returns an awaitable.

    Same shape and contract as :func:`wait_for` (see there for the
    ``probe_first`` / *interval* / *on_timeout* semantics); the clock is the
    running loop's (``loop.time()``), the sleep is ``asyncio.sleep``.
    """
    # Here, not at the top: every command's help imports this module, and
    # only a caller already on an event loop reaches this line.
    import asyncio

    loop = asyncio.get_running_loop()

    async def probe() -> bool:
        result = predicate()
        if inspect.isawaitable(result):
            return await result
        return result

    _check_timeout(timeout)
    deadline = loop.time() + timeout
    if probe_first and await probe():
        return
    sleeps = 0
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise WaitTimeoutError(_render_on_timeout(on_timeout))
        await asyncio.sleep(min(_interval_at(interval, sleeps), remaining))
        sleeps += 1
        if await probe():
            return


def _get_literal_values(
    type_: Any,
) -> list[TypeVar]:

    origin = get_origin(type_)
    if origin is Literal:
        return list(get_args(type_))
    if origin is Union:
        values: list[TypeVar] = []
        for arg in get_args(type_):
            values += _get_literal_values(arg)
        return values
    raise ValueError(f"{type_} is {origin}, not a Literal or Union of Literals")


@dataclass(frozen=True)
class Arg:
    """CLI overlay: force a parameter to a positional argument.

    ``variadic=True`` makes it a space-separated list of ``elem_type`` (used for
    Python-union list params Typer can't read, e.g. ``str | Sequence[...]``).
    ``elem_type`` also overrides the CLI type for a scalar union. ``name`` sets
    the argument's displayed metavar (e.g. ``Arg(name="SOURCE")`` shows
    ``SOURCE`` in ``--help`` instead of the parameter's Python name); it does
    not change how the argument is passed, since positional arguments have no
    flag. Imports no typer.
    """

    variadic: bool = False
    elem_type: type | None = None
    name: str | None = None
    help: str | None = None
    remote_path: 'Literal["any", "dir"] | None' = None
    """Complete this parameter as a path on the remote host (None = local).

    ``"any"`` offers files and directories; ``"dir"`` offers directories only.
    Consumed by ``otto.cli.param_synth``, which attaches the remote-path
    completer; carries no typer import itself.
    """


@dataclass(frozen=True)
class Opt:
    """CLI overlay: force a parameter to a ``--option``. Imports no typer.

    ``name`` overrides the synthesized flag (e.g. ``Opt(name="--dest")`` makes
    the CLI flag ``--dest`` regardless of the parameter's Python name); the
    value is still bound to the original Python parameter. When omitted, the
    flag defaults to ``--<param-name>`` as usual.
    """

    elem_type: type | None = None
    name: str | None = None
    short: str | None = None
    """A short alias (``"-r"``) added beside the long flag.

    The long flag is ``name`` when given, else the synthesized
    ``--<param-name>`` — so ``Opt(short="-r")`` on ``recursive`` yields
    ``--recursive`` and ``-r`` together, never ``-r`` alone. On a ``bool``
    parameter the synthesized long flag is written ``--recursive/--no-recursive``
    so the short alias never costs the option its auto-generated negative.
    """
    help: str | None = None
    min: float | None = None
    """Inclusive lower bound forwarded to click's numeric range, or None.

    Typer exposes only an inclusive ``min`` (no ``min_open``), so design the
    accepted range so an inclusive bound expresses it exactly.
    """
    remote_path: 'Literal["any", "dir"] | None' = None
    """Complete this option as a path on the remote host (None = local).

    ``"any"`` offers files and directories; ``"dir"`` offers directories only.
    Consumed by ``otto.cli.param_synth``, which attaches the remote-path
    completer; carries no typer import itself.
    """
    host_user: 'Literal["any", "direct"] | None' = None
    """Complete this option as a login the typed host declares (None = no completion).

    ``"any"`` offers every login (the verb replays proxy hops); ``"direct"``
    offers only logins with no proxy hop (``get``/``put`` authenticate as the
    user). A ``--term`` typed before the cursor narrows to creds scoped to
    that protocol. Consumed by ``otto.cli.param_synth``; carries no typer
    import itself.
    """


class _Exclude:
    """Sentinel: drop a parameter from the CLI (filled with its default)."""

    __slots__ = ()


Exclude = _Exclude()
"""Mark a verb parameter ``Annotated[T, Exclude]`` to keep it off the CLI.

The command line never sets it, so the verb runs with the parameter's
default. Python callers still pass it as usual."""


DRY_RUN_HEADLINE = "dry run: no command body was run and no device was contacted"
"""First line of every seam-default dry-run block (``otto.cli.invoke``)."""

DRY_RUN_HEADLINE_PROBED = (
    "dry run: no command body was run; --probe opened a connection only, and ran no command"
)
"""Replaces :data:`DRY_RUN_HEADLINE` when ``--dry-run --probe`` actually dialed.

Kept beside its sibling precisely because the two say incompatible things about
device contact: the default headline's "no device was contacted" is FALSE once
``--probe`` opens a transport, and a reader reworking either line needs to see
both at once. ``--probe`` is a CLI flag with no library equivalent, so only
``otto.cli.invoke`` prints this one.
"""


_CliVerb = TypeVar("_CliVerb", bound=Callable[..., Any])
"""The decorated verb's own type, threaded through ``cli_exposed`` unchanged.

``cli_exposed`` only *stamps* attributes and hands the function straight back,
so it must be typed to say so. Returning a bare ``Callable[..., Any]`` instead
makes every decorated verb ``Any`` at its call sites, hiding real argument
errors from ``ty`` (and from an editor) across every host class -- which is
what ``ty``'s ``dynamic-function-decorator-return`` rule reports.
"""


@overload
def cli_exposed(fn: _CliVerb) -> _CliVerb: ...


@overload
def cli_exposed(
    fn: None = None,
    *,
    name: str | None = None,
    help_: str | None = None,
    success: str | None = None,
    output_dir: bool = True,
    dry_run_preview: bool = False,
) -> Callable[[_CliVerb], _CliVerb]: ...


def cli_exposed(
    fn: _CliVerb | None = None,
    *,
    name: str | None = None,
    help_: str | None = None,
    success: str | None = None,
    output_dir: bool = True,
    dry_run_preview: bool = False,
) -> _CliVerb | Callable[[_CliVerb], _CliVerb]:
    """Mark a host coroutine method for auto-exposure as an ``otto host`` subcommand.

    ``name`` defaults to the method name with underscores dashed.
    ``help_`` is the one-line description ``otto host`` shows for the verb; it
    defaults to the first line of the method docstring, so give it whenever that
    line is written for API readers (reStructuredText, parameter references)
    rather than for someone typing ``otto host <id> <verb> --help``. An override
    that gives none inherits the ``help_`` of the verb it overrides.
    ``success`` is an optional message printed on a successful ``(Status, "")``
    result (e.g. "Transfer complete.").
    ``output_dir=False`` marks a read-only verb that creates no per-invocation
    output directory (e.g. ``exists``/``lsmod``); the default ``True`` keeps one.
    ``dry_run_preview=True`` opts the verb out of the CLI's seam default under
    ``--dry-run`` -- otto stops before an ordinary verb's body runs, but an
    opted-in verb runs its body so its own ``is_dry_run()`` branch can render a
    configuration-only preview. Default ``False``, so a verb author who never
    thought about dry runs still cannot contact a device under one.

    Usable bare (``@cli_exposed``) or called (``@cli_exposed(name=..., ...)``).
    """

    def deco(f: _CliVerb) -> _CliVerb:
        f.__cli_exposed__ = True  # ty: ignore[unresolved-attribute]
        f.__cli_name__ = name or f.__name__.replace("_", "-")  # ty: ignore[unresolved-attribute]
        f.__cli_help__ = help_  # ty: ignore[unresolved-attribute]
        f.__cli_success__ = success  # ty: ignore[unresolved-attribute]
        f.__cli_output_dir__ = output_dir  # ty: ignore[unresolved-attribute]
        f.__cli_dry_run_preview__ = dry_run_preview  # ty: ignore[unresolved-attribute]
        return f

    return deco(fn) if fn is not None else deco


T = TypeVar("T")


def is_literal(value: Any, literal_type: type[T]) -> T:
    """Raise a TypeError if value is not a valid member of the Literal type."""
    valid = _get_literal_values(literal_type)
    if value not in valid:
        raise TypeError(f"{value!r} is not a valid value. Expected one of: {valid}")
    return value


# TODO: Restructure this file into a directory names utils and then a file per group of functionality:  # noqa: E501 — TODO comment
# status for the below status enums
# types for the above str split on commas
class Status(Enum):
    """General status enum for commands and tests.

    >>> Status.Success
    <Status.Success: 0>
    >>> Status.Failed
    <Status.Failed: 1>
    >>> Status(0) is Status.Success
    True
    >>> Status.Skipped.is_ok, Status.NotRun.is_ok
    (True, False)
    """

    Success = 0
    Failed = 1
    Error = 2
    Unstable = 3
    Skipped = 4
    NotRun = 5
    """A dry run declined this command -- nothing issued, nothing measured.

    NOT ok, deliberately -- Skipped stays ok for genuine skips, but code that
    branches on a NotRun's is_ok must take its failure arm, so nothing
    proceeds on a fiction. See the design spec
    ``docs/superpowers/specs/2026-08-15-dry-run-contract-design.md`` #4.

    NO COLON in the summary line above, and that is load-bearing rather than
    style: napoleon parses an attribute docstring's ``prefix: rest`` as
    ``type: description``, so a colon here makes Sphinx look up the prefix as
    a class and ``-W -n`` fails the build on the missing target.
    """

    @property
    def is_ok(self) -> bool:
        """True for statuses that should be treated as passing (Success, Skipped)."""
        return self in (Status.Success, Status.Skipped)
