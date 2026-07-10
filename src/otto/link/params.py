"""Typed impairment parameters: unit parsing, merge, and coupling rules.

Spec §3.1/§3.3 (docs/superpowers/specs/2026-07-10-link-impairment-design.md):
bare time = milliseconds, bare percent = percent, rate REQUIRES an explicit tc
unit; re-impair merges per-param last-one-wins and an explicit zero clears just
that param.
"""

import re
from dataclasses import dataclass, fields

_TIME_RE = re.compile(r"^(?P<num>\d+(?:\.\d+)?)(?P<unit>us|ms|s)?$")
_PERCENT_RE = re.compile(r"^(?P<num>\d+(?:\.\d+)?)%?$")

# tc's full rate-unit vocabulary → bits-per-second multiplier. This is the ONE
# source of truth for both the accept-regex (parse_rate) AND canonicalization
# (canonical_key): SI decimal bit-forms (kbit=1000), byte-forms (bps = *8, so
# 1kbps = 8000 bit/s), and the IEC binary forms tc also accepts (kibit=1024,
# kibps=1024*8). See ``canonical_key`` for WHY canonicalization is needed
# (final-review Critical 2026-07-10).
_RATE_UNITS_BPS: dict[str, int] = {
    "bit": 1,
    "kbit": 1_000,
    "mbit": 1_000_000,
    "gbit": 1_000_000_000,
    "tbit": 1_000_000_000_000,
    "bps": 8,
    "kbps": 8_000,
    "mbps": 8_000_000,
    "gbps": 8_000_000_000,
    "tbps": 8_000_000_000_000,
    "kibit": 1_024,
    "mibit": 1_024**2,
    "gibit": 1_024**3,
    "tibit": 1_024**4,
    "kibps": 1_024 * 8,
    "mibps": 1_024**2 * 8,
    "gibps": 1_024**3 * 8,
    "tibps": 1_024**4 * 8,
}
# Longest unit first so the anchored full-match never stops short (e.g. `bit`
# swallowing only the tail of `kibit`).
_RATE_UNIT_ALT = "|".join(sorted(_RATE_UNITS_BPS, key=len, reverse=True))
_RATE_RE = re.compile(rf"^(?P<num>\d+(?:\.\d+)?)(?P<unit>{_RATE_UNIT_ALT})$")

_TIME_TO_MS = {"us": 0.001, "ms": 1.0, "s": 1000.0, None: 1.0}
_MAX_PERCENT = 100.0


def parse_time_ms(text: str, *, option: str) -> float:
    """Parse a time value in milliseconds; a bare number means ms (spec §3.1)."""
    m = _TIME_RE.match(text.strip().lower())
    if m is None:
        raise ValueError(
            f"{option} {text!r} is not a time value (bare number = ms; us/ms/s suffixes)"
        )
    return float(m.group("num")) * _TIME_TO_MS[m.group("unit")]


def parse_percent(text: str, *, option: str) -> float:
    """Parse a percentage; a bare number means percent (spec §3.1)."""
    m = _PERCENT_RE.match(text.strip())
    if m is None:
        raise ValueError(f"{option} {text!r} is not a percentage (bare number = percent)")
    value = float(m.group("num"))
    if value > _MAX_PERCENT:
        raise ValueError(f"{option} {value:g} is over 100%")
    return value


def parse_rate(text: str) -> str:
    """Parse a rate; an explicit tc unit is REQUIRED. Bare ``"0"`` clears (§3.3)."""
    cleaned = text.strip().lower()
    if cleaned == "0":
        return "0"
    if _RATE_RE.match(cleaned) is None:
        raise ValueError(
            f"--rate {text!r} needs an explicit unit (kbit/mbit/gbit/...) — "
            "there is no natural default for bandwidth"
        )
    return cleaned


def _fmt(value: float) -> str:
    return f"{value:g}"


@dataclass(frozen=True, slots=True)
class ImpairmentParams:
    """One impairment parameter set. ``None`` = not set/absent."""

    delay_ms: float | None = None
    jitter_ms: float | None = None
    loss_pct: float | None = None
    corrupt_pct: float | None = None
    duplicate_pct: float | None = None
    reorder_pct: float | None = None
    rate: str | None = None
    """Canonical lowercase tc rate string (``"10mbit"``); ``"0"`` = clear."""

    def is_empty(self) -> bool:
        """Return ``True`` if every param is unset."""
        return all(getattr(self, f.name) is None for f in fields(self))

    def merged_over(self, base: "ImpairmentParams") -> "ImpairmentParams":
        """Per-param last-one-wins over *base*; explicit zeros clear (spec §3.3)."""
        merged: dict[str, float | str | None] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if value is None:
                value = getattr(base, f.name)
            if value in (0.0, "0"):
                value = None
            merged[f.name] = value
        return ImpairmentParams(**merged)  # ty: ignore[invalid-argument-type]

    def validate(self) -> None:
        """Enforce netem coupling rules.

        Evaluate AFTER merge — a jitter or reorder given now may be joining
        a delay that was already applied in an earlier impair call.
        """
        if self.jitter_ms is not None and self.delay_ms is None:
            raise ValueError("--jitter requires a delay (given now or already applied)")
        if self.reorder_pct is not None and self.delay_ms is None:
            raise ValueError("--reorder requires a delay (given now or already applied)")

    def describe(self) -> str:
        """Human/argv-shaped summary with explicit units, e.g. ``delay 50ms loss 2%``."""
        parts: list[str] = []
        if self.delay_ms is not None:
            token = f"delay {_fmt(self.delay_ms)}ms"
            if self.jitter_ms is not None:
                token += f" {_fmt(self.jitter_ms)}ms"
            parts.append(token)
        if self.loss_pct is not None:
            parts.append(f"loss {_fmt(self.loss_pct)}%")
        if self.corrupt_pct is not None:
            parts.append(f"corrupt {_fmt(self.corrupt_pct)}%")
        if self.duplicate_pct is not None:
            parts.append(f"duplicate {_fmt(self.duplicate_pct)}%")
        if self.reorder_pct is not None:
            parts.append(f"reorder {_fmt(self.reorder_pct)}%")
        if self.rate is not None:
            parts.append(f"rate {self.rate}")
        return " ".join(parts)


def _canonical_rate_bps(rate: str | None) -> int | None:
    """Canonical integer bits-per-second for a stored rate string (``None`` stays ``None``)."""
    if rate is None:
        return None
    if rate == "0":
        return 0
    m = _RATE_RE.match(rate)
    if m is None:
        raise ValueError(f"cannot canonicalize rate {rate!r}: not a tc rate string")
    return round(float(m.group("num")) * _RATE_UNITS_BPS[m.group("unit")])


def canonical_key(params: ImpairmentParams) -> tuple[int | None, ...]:
    """Spelling-independent identity for *params* — compare MEANING, not text.

    tc canonicalizes on display, so the exact string otto applies is NOT the
    string it reads back: ``1.5mbit``->``1500Kbit``, ``10mbps``->``80Mbit``
    (bytes to bits, times 8), ``>=1s`` in ``%g`` seconds. A post-apply verify
    (and any equality that must survive a kernel round-trip) therefore has to
    canonicalize both sides first, or it false-fails on documented inputs.
    Times collapse to integer microseconds, percents to integer
    milli-percent, rate to integer bits-per-second via the
    ``_RATE_UNITS_BPS`` multiplier map; ``round`` scrubs float dust
    (``700 * 0.001 == 0.7000000000000001``) — final-review Critical 2026-07-10.

    Whole-millisecond delay/jitter values are tick-aligned (netem quantizes
    to 64ns psched ticks, and 1ms = 1,000,000ns divides evenly by 64) and
    round-trip through this key exactly. Sub-millisecond values do NOT:
    ``0.7ms`` is the QUANTIZATION example, not a round-trip-stable one — on
    real hardware it reads back as ``699us``, a genuine ~1us kernel-tick
    delta rather than a spelling reformat (observed live, 2026-07-10). This
    key still records that delta exactly (it must — it is the ONE place
    meaning is captured); :func:`equivalent` is where the tick-quantization
    tolerance for the delay/jitter slots lives.
    """

    def _us(ms: float | None) -> int | None:
        return None if ms is None else round(ms * 1000)

    def _mpct(pct: float | None) -> int | None:
        return None if pct is None else round(pct * 1000)

    return (
        _us(params.delay_ms),
        _us(params.jitter_ms),
        _mpct(params.loss_pct),
        _mpct(params.corrupt_pct),
        _mpct(params.duplicate_pct),
        _mpct(params.reorder_pct),
        _canonical_rate_bps(params.rate),
    )


def _time_close(x: int | None, y: int | None) -> bool:
    """Tick-quantization tolerance for one canonicalized time (µs) slot.

    ``None`` is only close to ``None`` — a missing field is never
    tolerance-close to a present one, so this can't hide a field that
    genuinely failed to apply. Otherwise: ``abs(x - y) <= max(2, 0.5% of the
    larger value)``. See :func:`equivalent` for the full WHY.
    """
    if x is None or y is None:
        return x == y
    return abs(x - y) <= max(2, 0.005 * max(x, y))


def equivalent(a: ImpairmentParams, b: ImpairmentParams) -> bool:
    """Return ``True`` when *a* and *b* mean the same impairment (see :func:`canonical_key`).

    The delay/jitter (TIME) slots of the canonical key compare with a small
    tolerance instead of exact equality; every other slot (percent, rate)
    still compares exactly. WHY: netem quantizes delay/jitter to 64ns
    kernel (psched) ticks and tc's display is microsecond-truncated, so a
    value like ``700us`` can read back as ``699us`` on real hardware — a
    genuine kernel-tick artifact, observed live 2026-07-10, not a spelling
    reformat (:func:`canonical_key` already handles those). The tolerance
    is ``max(2us, 0.5% of the larger value)``: the 2us floor is 2x the
    observed ~1us quantization delta, and the 0.5% relative band covers
    tc's ``%g`` display rounding at second scale (see the second-scale
    cases in ``TestTickQuantizationTolerance``).

    This cannot mask a real mismatch: an impairment that genuinely failed
    to apply (or applied as a different value) reads back as ``None``
    (field missing), the PRIOR value (merge never took), or a different
    field entirely — never a same-field sub-percent delta on the value
    that WAS actually applied. ``None`` is only equivalent to ``None``.
    """
    ka = canonical_key(a)
    kb = canonical_key(b)
    delay_a, jitter_a, *rest_a = ka
    delay_b, jitter_b, *rest_b = kb
    return _time_close(delay_a, delay_b) and _time_close(jitter_a, jitter_b) and rest_a == rest_b
