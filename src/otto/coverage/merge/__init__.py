"""Coverage merge: LCOV loading, multi-source merging, and source-path normalisation."""

from .lcov_loader import LCOVLoader
from .merger import LcovMerger
from .paths import PathMapping, PathRemapper

__all__ = ["LCOVLoader", "LcovMerger", "PathMapping", "PathRemapper"]
