"""Typed coverage-pipeline errors carrying user-actionable messages."""


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
