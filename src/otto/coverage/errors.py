"""Typed coverage-pipeline errors carrying user-actionable messages."""


class CoverageToolVersionError(RuntimeError):
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


class CoverageDataMismatchError(RuntimeError):
    """Fetched ``.gcda`` data does not match the current build's ``.gcno`` notes.

    gcov embeds a build stamp in both files; a (partial) rebuild of the
    product between ``otto test --cov`` and ``otto cov report`` changes the
    stamps, and gcov refuses the pairing (``stamp mismatch with notes file``).
    """

    def __init__(self, detail: str) -> None:
        """Frame *detail* (raw lcov/gcov output) with the likely cause and remedy."""
        super().__init__(
            "Coverage data does not match the current product build (gcov "
            "reports a stamp mismatch between .gcda data and .gcno notes "
            "files). The product was likely rebuilt after `otto test --cov` "
            "collected this data — coverage must be reported against the "
            "exact build that produced it. Re-run `otto test --cov` and "
            "report on the new output directory.\n"
            f"Underlying output:\n{detail}"
        )
