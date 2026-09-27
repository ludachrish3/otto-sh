"""The verdict vocabulary every otto check reports in (spec 2026-09-24 §3.1).

``fail`` and ``unsupported`` are the split otto's developers need most from a
user's report: ``unsupported`` means the host's tool or kernel REJECTED the
command (a missing capability), ``fail`` means it accepted the command and the
measurement came out wrong (otto, or the host, misbehaves). ``unmeasured`` is
"no evidence either way" and always says why.
"""

import enum
from dataclasses import dataclass, field


class Verdict(enum.Enum):
    """One check result; only ``fail`` and ``unsupported`` fail a run."""

    PASS = "pass"  # noqa: S105 — the verdict word, not a credential
    FAIL = "fail"
    UNSUPPORTED = "unsupported"
    UNMEASURED = "unmeasured"
    SKIPPED = "skipped"

    @property
    def fails(self) -> bool:
        """True when this verdict makes the command exit 1."""
        return self in (Verdict.FAIL, Verdict.UNSUPPORTED)


class UnmeasuredReason(enum.Enum):
    """Why an ``unmeasured`` result has no evidence either way."""

    MISSING_TOOL = "missing-tool"
    NOISY_BASELINE = "noisy-baseline"
    NO_REPLY_ORACLE = "no-reply-oracle"
    NO_CLOCK = "no-clock"


@dataclass(frozen=True)
class FeatureResult:
    """One feature's verdict plus the evidence a reader needs to act on it.

    *measured* and *wanted* are display strings (``"+200.4ms"``, ``"200 ±25"``)
    because the renderer and the report both show them verbatim; *commands* are
    the exact commands otto ran for this row and *output* the relevant raw
    output (tc's stderr for ``unsupported``, the probe output behind ``-v``).
    """

    feature: str
    verdict: Verdict
    reason: UnmeasuredReason | None = None
    measured: str | None = None
    wanted: str | None = None
    detail: str | None = None
    commands: list[str] = field(default_factory=list)
    output: str | None = None
    hint: str | None = None

    def __post_init__(self) -> None:
        """Enforce: a reason exactly when the verdict is ``unmeasured``."""
        if self.verdict is Verdict.UNMEASURED and self.reason is None:
            raise ValueError(f"{self.feature}: unmeasured needs a reason")
        if self.verdict is not Verdict.UNMEASURED and self.reason is not None:
            raise ValueError(f"{self.feature}: only unmeasured carries a reason")


def count_verdicts(results: list[FeatureResult]) -> dict[Verdict, int]:
    """Count *results* per verdict, every verdict present, in enum order."""
    counts = dict.fromkeys(Verdict, 0)
    for result in results:
        counts[result.verdict] += 1
    return counts


class ReportVerdicts:
    """The verdict roll-up every check report shares: what failed, and whether the run is ok.

    A report supplies :meth:`results` and a ``refusal`` field; this adds the
    rest, so ``otto link check`` and ``otto tunnel check`` judge a run alike.
    """

    refusal: str | None

    def results(self) -> list[FeatureResult]:
        """Every result the report holds."""
        raise NotImplementedError

    def failed(self) -> list[FeatureResult]:
        """Return the results that fail the run (``fail`` or ``unsupported``)."""
        return [r for r in self.results() if r.verdict.fails]

    @property
    def ok(self) -> bool:
        """True when the check was not refused and nothing failed."""
        return self.refusal is None and not self.failed()
