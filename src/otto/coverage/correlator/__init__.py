"""Coverage correlator: LCOV loading, multi-source merging, and source-path normalisation."""

from .lcov_loader import LCOVLoader
from .merger import LcovMerger
from .paths import PathCorrelator, PathMapping

__all__ = ["LCOVLoader", "LcovMerger", "PathCorrelator", "PathMapping"]
