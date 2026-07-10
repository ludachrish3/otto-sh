"""NetEm — the first-party ``LinkImpairer`` (tc qdisc netem on unix hosts).

argv builders ALWAYS emit explicit units (spec §3.1) — tc's bare-number
semantics vary by parameter and iproute2 version. The parser reads
``tc qdisc show dev X`` back into :class:`~otto.link.params.ImpairmentParams`
(kernel qdisc config is the only state — spec §6) and tolerates both modern
and old iproute2 formatting (``50ms`` vs ``50.0ms``).
"""

import re
from typing import ClassVar

from typing_extensions import override

from .impairer import LinkImpairer, register_impairer
from .params import ImpairmentParams

_TIME_TOKEN = re.compile(r"^(?P<num>\d+(?:\.\d+)?)(?P<unit>us|usec|ms|msec|s|sec)$")
_PERCENT_TOKEN = re.compile(r"^(?P<num>\d+(?:\.\d+)?)%$")
_TIME_TO_MS = {"us": 0.001, "usec": 0.001, "ms": 1.0, "msec": 1.0, "s": 1000.0, "sec": 1000.0}
_PERCENT_KEYWORDS = {
    "loss": "loss_pct",
    "corrupt": "corrupt_pct",
    "duplicate": "duplicate_pct",
    "reorder": "reorder_pct",
}
_MIN_DELAY_TOKENS = 4
"""``qdisc netem <handle>: root ...`` — the shortest possible root-netem line."""


def netem_args(params: ImpairmentParams) -> str:
    """Render *params* as netem qdisc arguments with explicit units."""
    return params.describe()


def _parse_time(token: str) -> float | None:
    m = _TIME_TOKEN.match(token)
    return float(m.group("num")) * _TIME_TO_MS[m.group("unit")] if m else None


def _parse_percent_token(token: str) -> float | None:
    m = _PERCENT_TOKEN.match(token)
    return float(m.group("num")) if m else None


def parse_qdisc_show(output: str) -> ImpairmentParams | None:
    """Parse ``tc qdisc show dev X`` output; ``None`` = no root netem qdisc."""
    for line in output.splitlines():
        tokens = line.split()
        if (
            len(tokens) >= _MIN_DELAY_TOKENS
            and tokens[0] == "qdisc"
            and tokens[1] == "netem"
            and "root" in tokens
        ):
            return _parse_netem_tokens(tokens)
    return None


def _parse_netem_tokens(tokens: list[str]) -> ImpairmentParams:
    kw: dict[str, float | str | None] = {}
    i = 0
    while i < len(tokens):
        word = tokens[i]
        if word == "delay" and i + 1 < len(tokens):
            kw["delay_ms"] = _parse_time(tokens[i + 1])
            if i + 2 < len(tokens):
                jitter = _parse_time(tokens[i + 2])
                if jitter is not None:
                    kw["jitter_ms"] = jitter
                    i += 1
            i += 2
            continue
        if word in _PERCENT_KEYWORDS and i + 1 < len(tokens):
            kw[_PERCENT_KEYWORDS[word]] = _parse_percent_token(tokens[i + 1])
            i += 2
            continue
        if word == "rate" and i + 1 < len(tokens):
            kw["rate"] = tokens[i + 1].lower()
            i += 2
            continue
        i += 1
    return ImpairmentParams(**{k: v for k, v in kw.items() if v is not None})  # ty: ignore[invalid-argument-type]


class NetEmImpairer(LinkImpairer):
    """tc/netem on a unix host's interface."""

    host_families: ClassVar[frozenset[str]] = frozenset({"unix"})

    @override
    def apply_command(self, netdev: str, params: ImpairmentParams) -> str:
        """Idempotent ``tc qdisc replace`` command applying *params* to *netdev*."""
        return f"tc qdisc replace dev {netdev} root netem {netem_args(params)}"

    @override
    def read_command(self, netdev: str) -> str:
        """``tc qdisc show`` command; output is understood by :func:`parse_qdisc_show`."""
        return f"tc qdisc show dev {netdev}"

    @override
    def clear_command(self, netdev: str) -> str:
        """``tc qdisc del`` command removing the root netem qdisc from *netdev*."""
        return f"tc qdisc del dev {netdev} root"

    @override
    def parse_read(self, output: str) -> ImpairmentParams | None:
        """Parse :meth:`read_command` output via :func:`parse_qdisc_show`."""
        return parse_qdisc_show(output)


register_impairer("netem", NetEmImpairer)
