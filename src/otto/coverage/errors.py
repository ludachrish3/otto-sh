"""Typed coverage-pipeline errors carrying user-actionable messages."""

from typing import TYPE_CHECKING

from ..config.coverage_settings import CoverageConfigError
from ..errors import OttoError

if TYPE_CHECKING:  # pragma: no cover — typing only; see CoverageNotInstrumentedError
    from .instrumentation import InstrumentationReport


class CoverageToolVersionError(OttoError, RuntimeError):
    """The gcov tool used for capture cannot read this build's coverage format.

    geninfo probes the gcov tool and refuses when the ``.gcda``/``.gcno``
    were written by a different compiler family or version ("Incompatible
    GCC/GCOV version"). The classic trigger: the product was built with
    ``clang --coverage`` — clang stamps the GCC 4.8-era file format only
    ``llvm-cov gcov`` still reads — but capture ran GNU gcov. A cross-GCC
    build captured with the system gcov fails the same way.
    """

    def __init__(self, detail: str) -> None:
        """Frame *detail* (raw lcov/geninfo output) with the likely cause and remedy."""
        super().__init__(
            "The gcov tool cannot read this build's coverage data (geninfo "
            "reports an incompatible GCC/GCOV version). If the product was "
            "built with clang --coverage, capture needs `llvm-cov gcov`: "
            "install llvm so otto can auto-discover it, or set the host "
            "toolchain's gcov to an llvm-cov path. For a cross-GCC build, "
            "set it to the matching cross gcov instead.\n"
            f"Underlying output:\n{detail}"
        )


class CoverageNotInstrumentedError(CoverageConfigError):
    """Coverage was requested but no product on any coverage host is instrumented.

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


class CoverageDataMismatchError(OttoError, RuntimeError):
    """Fetched ``.gcda`` data does not match the current build's ``.gcno`` notes.

    gcov embeds a build stamp in both files; a (partial) rebuild of the
    product between ``otto test --cov`` and ``otto cov report`` changes the
    stamps and breaks the pairing. Raised by the merger's structural
    pairing check before lcov runs — header stamps, plus clang-dialect
    per-function checksums (the only detection possible for clang builds,
    where llvm-cov zeroes the data silently) — or from GNU gcov's own
    ``stamp mismatch with notes file`` refusal as the backstop.
    """

    def __init__(self, detail: str) -> None:
        """Frame *detail* (mismatch lines or raw lcov output) with cause and remedy."""
        super().__init__(
            "Coverage data does not match the current product build (the "
            ".gcda data and .gcno notes files carry different build "
            "stamps). The product was likely rebuilt after `otto test "
            "--cov` collected this data — coverage must be reported "
            "against the exact build that produced it. Re-run `otto test "
            "--cov` and report on the new output directory.\n"
            f"Underlying output:\n{detail}"
        )
