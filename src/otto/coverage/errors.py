"""Typed coverage-pipeline errors carrying user-actionable messages."""

from typing import TYPE_CHECKING

from ..config.coverage_settings import CoverageConfigError
from ..errors import FieldError, OttoError

if TYPE_CHECKING:  # pragma: no cover — typing only; see CoverageNotInstrumentedError
    from .capture.produce import ProduceResult, ProductCapture
    from .instrumentation import InstrumentationReport
    from .reports import CleanReport


class CoverageToolVersionError(OttoError, RuntimeError):
    """The gcov tool used for capture cannot read this build's coverage format.

    geninfo probes the gcov tool and refuses when the ``.gcda``/``.gcno``
    carry a gcov stamp the tool does not read ("Incompatible GCC/GCOV
    version"). The classic trigger: data stamped ``402*`` or ``408*``, which
    only ``llvm-cov gcov`` reads (``clang --coverage`` up to clang 20),
    captured with GNU gcov. A cross-GCC build captured with the system gcov
    fails the same way.
    """

    def __init__(
        self,
        detail: str,
        *,
        setting: str = "the host toolchain's gcov",
        subject: str = "the product",
    ) -> None:
        """Frame *detail* (raw lcov/geninfo output) with the likely cause and remedy.

        *setting* is where the reader is named by hand: a host's
        ``toolchain.gcov`` for a product, a unit tier's
        ``coverage.tiers.<tier>.toolchain.coverage_tool`` for a unit capture.
        *subject* is what was built: ``the product``, or a unit capture's
        ``the tested code``.
        """
        super().__init__(
            "The gcov tool cannot read this build's coverage data (geninfo "
            f"reports an incompatible GCC/GCOV version). If {subject} was "
            "built with clang --coverage, capture needs `llvm-cov gcov`: "
            f"install llvm so otto can auto-discover it, or set {setting} "
            "to an llvm-cov path. For a cross-GCC build, "
            "set it to the matching cross gcov instead.\n"
            f"Underlying output:\n{detail}"
        )
        self.detail = detail


class CoverageNotInstrumentedError(CoverageConfigError):
    """Coverage was requested but no product on any coverage host is instrumented.

    The unit tier raises it too, before any capture of a report, when a unit
    view selects no product or a product it filters by is not instrumented.

    Raised before anything runs, with the full per-product verdict listing in
    the message, so the run is never spent producing counters that do not
    exist.

    ``report`` carries the same verdicts as STRUCTURE
    (``otto.coverage.instrumentation.InstrumentationReport``) so a console
    caller can render them as the Rich table, while ``str(self)`` keeps the
    plain listing a log file and a non-CLI caller can actually use. It
    defaults to ``None``: every ``CoverageNotInstrumentedError("...")``
    construction, its pickling, and its ``str()`` are unchanged by it.

    The annotation is a string under :data:`typing.TYPE_CHECKING`, never a
    runtime import: ``otto.coverage.errors`` sits on the ``cov`` and ``test``
    import surfaces and ``otto.coverage.instrumentation`` pulls
    ``rich.table``, so importing the producing module here for real would
    widen exactly the edge the import budget exists to keep narrow.

    Only *message* reaches ``BaseException.args``, so an unpickled copy reads
    identically and carries ``report = None`` — the structure is a
    same-process rendering aid, not part of the error's identity.
    """

    def __init__(self, message: str, report: "InstrumentationReport | None" = None) -> None:
        """Record *report* (the per-product verdicts, or ``None``) beside *message*."""
        super().__init__(message)
        self.report = report


class NoCoverageDataError(OttoError, ValueError):
    """No ``.gcda`` counters were retrieved from any matched product.

    Raised by ``otto.coverage.collect.collect_coverage`` after the remote-fetch
    and embedded-collection stages both complete with nothing to show for it
    — no instrumented product on any host the ``[coverage].hosts`` selector
    matched (or, with no selector, any host in the lab) contributed coverage
    data. The message names every ``host:product:cov_dir`` triple searched,
    or says that no host carried an instrumented product at all.
    """


class CoverageInputError(FieldError, ValueError):
    """A coverage verb's input is unusable; nothing was touched.

    ``field`` is the parameter at fault: ``tier``, ``ticket`` or
    ``output_dir`` (:func:`~otto.coverage.get.get_coverage`), ``cov_dirs``
    (stale coverage metadata too) or ``tier_specs``
    (:func:`~otto.coverage.reporter.run_coverage_report`).
    """


class NoCoverageHostsError(OttoError, ValueError):
    """``[coverage].hosts`` selects no host a coverage walk includes: nothing to collect."""


class CoverageCleanError(OttoError, RuntimeError):
    """Some coverage counters could not be cleared; its ``report`` attribute says which."""

    def __init__(self, report: "CleanReport") -> None:
        """Frame *report*'s per-host/product failures as one summary message."""
        reasons = "; ".join(f"{f.key.label}: {f.reason}" for f in report.failed)
        super().__init__(f"could not clear coverage counters: {reasons}")
        self.report = report


class CoverageDataMismatchError(OttoError, RuntimeError):
    """Fetched ``.gcda`` data cannot be decoded against its ``.gcno`` notes.

    gcov embeds a build stamp in both files; a (partial) rebuild of the
    product after it was deployed changes the notes' stamps and breaks the
    pairing. Raised at capture by the structural pairing check before lcov
    runs — header format version and stamps, plus per-function checksums for
    the stamps llvm-cov reads (the only detection possible there, since
    llvm-cov zeroes the data silently) — or from GNU gcov's own ``stamp
    mismatch with notes file`` refusal as the backstop.
    """

    def __init__(self, detail: str) -> None:
        """Frame *detail* (mismatch lines or raw lcov output) with cause and remedy."""
        super().__init__(
            "Coverage data does not match the current product build: each .gcda "
            "data file below cannot be decoded against its .gcno notes file. "
            "Counters are only meaningful against the exact build that produced "
            "them, and the usual cause is a product rebuilt after it was deployed. "
            "Restore the build that ran and run `otto cov get` again, or redeploy "
            "the current build and re-run the tests (`otto test --cov`).\n"
            f"Underlying output:\n{detail}"
        )


class CaptureFailedError(OttoError, RuntimeError):
    """``lcov --capture`` failed; the message is the short failure, not the full output."""


class CaptureCommitError(OttoError, RuntimeError):
    """An e2e capture was taken at a different commit than the tree is at now."""

    def __init__(self, *, capture: str, taken_at: str, head: str) -> None:
        """Name the capture, both commits and the two ways out."""
        super().__init__(
            f"The e2e capture {capture} was taken at commit {taken_at[:12]}, but the tree is "
            f"at {head[:12]}. Coverage is reported against the commit it was taken at: "
            f"check that commit out (`git checkout {taken_at[:12]}`) and report again, or "
            "re-run `otto test --cov` at the current commit."
        )
        self.taken_at = taken_at
        self.head = head


def _failure_of(failed: "ProductCapture") -> tuple[str, bool]:
    """*failed*'s failure, and whether it already leads with its ``<host>/<product>:`` label.

    Pairing and mismatch failures name their product themselves; the one
    place that is checked.
    """
    failure = failed.failure or ""
    return failure, failure.startswith(f"{failed.key.label}:")


def product_failure_line(failed: "ProductCapture") -> str:
    """One failed product's entry: ``host/product: fetch failed: …`` (or ``failed to capture``).

    What a run's coverage tail logs once per failed product (one warning per
    product; a capture failure's reason may span lines), and how a failed
    fetch reads wherever it is named. A remedy the product carries (a missing
    declaration, say) follows the reason.
    """
    label = f"{failed.key.label}:"
    failure, labelled = _failure_of(failed)
    if labelled:
        failure = failure[len(label) :].lstrip()
    verb = "fetch failed" if not failed.fetched else "failed to capture"
    line = f"{label} {verb}: {failure}"
    return f"{line}; {failed.remedy}" if failed.remedy else line


class CoverageCaptureError(OttoError, RuntimeError):
    """One or more products failed to be fetched or captured; the others captured normally.

    ``result`` carries every product's outcome, so a caller can still use
    (and clean) the products that captured before re-raising. ``details``
    holds each failed product's short failure, led by its ``host/product``
    label, for a caller that frames them its own way. ``clean`` is
    the clean of those captured products, when the raiser ran one before
    re-raising: a failed reset there is a failure of its own, and a caller
    that fails on a failed clean checks it here as it would on a success.

    The usual remedy, fixing the cause and running ``otto cov get`` again, is
    said once; a product that needs something else (a missing declaration)
    says so in its own entry, and is left out of that sentence.
    """

    def __init__(self, result: "ProduceResult", clean: "CleanReport | None" = None) -> None:
        """Frame each failed product's short failure as one message."""
        blocks = []
        for p in result.failed:
            failure, labelled = _failure_of(p)
            if not p.fetched or p.remedy:
                blocks.append(product_failure_line(p))
            else:
                blocks.append(failure if labelled else f"{p.key.label}:\n{failure}")
        fetches = sum(not p.fetched for p in result.failed)
        what = (
            "failed to capture"
            if not fetches
            else "could not be fetched"
            if fetches == len(blocks)
            else "failed to fetch or capture"
        )
        rerun = [p.key.label for p in result.failed if not p.remedy]
        if len(rerun) == len(blocks):
            message = (
                f"{len(blocks)} product(s) {what}; their counters stay on the host — "
                "fix the cause and run `otto cov get` again:\n\n" + "\n\n".join(blocks)
            )
        else:
            message = (
                f"{len(blocks)} product(s) {what}; their counters stay on the host:\n\n"
                + "\n\n".join(blocks)
            )
            if rerun:
                message += (
                    "\n\nFix the cause and run `otto cov get` again for: " + ", ".join(rerun) + "."
                )
        super().__init__(message)
        self.details = blocks
        self.result = result
        self.clean = clean


class PairingError(OttoError, ValueError):
    """A fetched ``.gcda`` could not be paired with exactly one ``.gcno``.

    ``kind`` names the case: ``merged-on-device`` (several objects wrote one
    file on the device), ``several-trees`` (several differing notes files all
    match), ``no-notes`` (no notes file matches).
    """

    def __init__(self, kind: str, message: str) -> None:
        """Record *kind* beside *message*."""
        super().__init__(message)
        self.kind = kind
