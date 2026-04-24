"""Parse lcov ``.info`` files and load them into a :class:`CoverageStore`.

The lcov ``.info`` format is stable and well-documented:
https://manpages.ubuntu.com/manpages/focal/man1/geninfo.1.html

Format summary::

    TN:<test name>
    SF:<source file path>
    FN:<line>,<function name>
    FNDA:<count>,<function name>
    DA:<line>,<count>[,<checksum>]
    BRDA:<line>,<block>,<branch>,<taken>   taken='-' means never reached
    BRH:<hit>,<found>
    end_of_record
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..store.model import (
    BranchHits,
    CoverageStore,
    FileRecord,
)
from .paths import PathCorrelator

logger = logging.getLogger(__name__)


class LCOVLoader:
    """Parse ``.info`` files and load them into a :class:`CoverageStore`
    under a named tier.

    Tier names are free-form strings.  The loader registers each tier
    with the store on first use so the store's ``tier_order`` reflects
    the data without callers having to pre-declare every tier.

    Example::

        store = CoverageStore(tier_order=["unit", "system", "manual"])
        correlator = PathCorrelator([...])
        loader = LCOVLoader(store, correlator)

        loader.load("system_merged.info", "system")
        loader.load("unit_tests.info",    "unit")
    """

    def __init__(self, store: CoverageStore, correlator: PathCorrelator) -> None:
        self.store = store
        self.correlator = correlator

    def load(self, info_path: Path | str, tier: str) -> int:
        """Load an ``.info`` file into the store under *tier*.

        Returns the number of source files loaded.
        """
        info_path = Path(info_path)
        logger.info("Loading %s as %s coverage", info_path.name, tier)
        self.store.register_tier(tier)

        current_file: FileRecord | None = None
        files_loaded = 0

        with open(info_path) as f:
            for raw_line in f:
                line = raw_line.strip()

                if line.startswith("SF:"):
                    raw_path = line[3:]
                    resolved = self.correlator.resolve(raw_path)
                    if resolved is None:
                        logger.warning("Unmapped path, using raw: %s", raw_path)
                        resolved = Path(raw_path)
                    current_file = self.store.get_or_create_file(resolved)

                elif line.startswith("DA:") and current_file is not None:
                    parts = line[3:].split(",")
                    lineno = int(parts[0])
                    count = int(parts[1])
                    lr = current_file.get_or_create_line(lineno)
                    lr.hits.add(tier, count)

                elif line.startswith("BRDA:") and current_file is not None:
                    parts = line[5:].split(",")
                    lineno = int(parts[0])
                    block = int(parts[1])
                    branch = int(parts[2])
                    taken = parts[3]

                    reachable = taken != "-"
                    count = int(taken) if reachable else 0

                    lr = current_file.get_or_create_line(lineno)
                    key = (block, branch)
                    existing_map = {(b.block, b.branch): b for b in lr.branches}

                    if key not in existing_map:
                        bh = BranchHits(block=block, branch=branch)
                        lr.branches.append(bh)
                        existing_map[key] = bh

                    bh = existing_map[key]
                    bh.set_reachable(tier, reachable)
                    if reachable and count > 0:
                        bh.hits.add(tier, count)

                elif line == "end_of_record":
                    if current_file is not None:
                        files_loaded += 1
                    current_file = None

        logger.info("Loaded %d files from %s", files_loaded, info_path.name)
        return files_loaded
