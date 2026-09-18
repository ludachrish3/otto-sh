r"""Parse lcov ``.info`` files into a :class:`~otto.coverage.store.model.CoverageStore`.

The lcov ``.info`` format is stable and well-documented:
https://manpages.ubuntu.com/manpages/focal/man1/geninfo.1.html

Format summary::

    TN:<test name>
    SF:<source file path>
    FN:<start>[,<end>],<function name>
    FNDA:<count>,<function name>
    DA:<line>,<count>[,<checksum>]
    BRDA:<line>,[e]<block>,<branch>,<taken>   taken='-' means never reached
    BRH:<hit>,<found>
    end_of_record

``BRDA``'s block field carries an optional leading ``e``: ``man geninfo``
places it before the *line number* (``BRDA:<exception tag><line
number>,<block number>,...``), but that is wrong — lcov 2.0's own writer
(``lcovutil.pm`` ~5093, ``printf(INFO_HANDLE "BRDA:%u,%s%u,%s,%s\n", $line,
$br->is_exception() ? 'e' : '', $block_id, ...)``) and its own parser
(``lcovutil.pm`` ~4725, ``/^BRDA:(\d+),(e?)(\d+),(.+)$/``) agree the tag
sits between the line number and the block number, exactly where this
module's format summary puts it. "'exception tag' is 'e' if this is a
branch related to exception handling" (``man geninfo``). otto's store has
no exception-branch concept, and these are compiler-synthesised arcs the
product never wrote (observed live: arm64 kernel headers'
``alternative-macros.h`` asm-goto alternatives, pulled into a kmod
product's translation unit) — see ``_parse_block_id`` below.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from ..store.model import (
    BranchHits,
    CoverageStore,
    FileRecord,
    FunctionRecord,
)
from .paths import PathRemapper

logger = logging.getLogger(__name__)

_FN_END_PREFIX = re.compile(r"(\d+),")


@dataclass
class FunctionHit:
    """One ``FNDA:`` record: hit count for a named function."""

    count: int
    name: str


def parse_fn_record(body: str) -> FunctionRecord:
    """Parse the text after ``FN:``.

    lcov 1.x writes ``<start>,<name>``; lcov ≥ 2.0 writes ``<start>,<end>,<name>``.
    A C++ name can itself contain commas (``std::pair<int, char> make(int,
    char)``), so this splits positionally from the left and never on every
    comma: ``start`` up to the first comma; an ``<end>`` only when the remainder
    starts with digits followed by a comma; everything after is the name.
    """
    start_text, _, rest = body.partition(",")
    end: int | None = None
    matched = _FN_END_PREFIX.match(rest)
    if matched is not None:
        end = int(matched.group(1))
        rest = rest[matched.end() :]
    return FunctionRecord(name=rest, start_line=int(start_text), end_line=end)


def parse_fnda_record(body: str) -> FunctionHit:
    """Parse the text after ``FNDA:`` — ``<count>,<name>``, split once."""
    count_text, _, name = body.partition(",")
    return FunctionHit(count=int(count_text), name=name)


def _parse_block_id(text: str) -> int | None:
    """Parse a ``BRDA:`` block field, or ``None`` for an exception-tagged branch.

    The field is ``[e]<digits>`` (see the module docstring, and lcov's own
    writer/parser cited there): a leading ``e`` marks the branch as
    exception-handling machinery the compiler synthesised, not a branch the
    product's own source wrote. otto's store has no exception-branch tier,
    so the caller drops the whole record on ``None`` rather than storing it
    under a guessed block id. An untagged id is always plain decimal — no
    fallback base is needed or applied.
    """
    if text.startswith("e"):
        return None
    return int(text)


class LCOVLoader:
    """Parse ``.info`` files into a :class:`~otto.coverage.store.model.CoverageStore` under a tier.

    Tier names are free-form strings.  The loader registers each tier
    with the store on first use so the store's ``tier_order`` reflects
    the data without callers having to pre-declare every tier.

    Example::

        store = CoverageStore(tier_order=["unit", "system", "manual"])
        remapper = PathRemapper([...])
        loader = LCOVLoader(store, remapper)

        loader.load("system_merged.info", "system")
        loader.load("unit_tests.info", "unit")
    """

    def __init__(self, store: CoverageStore, remapper: PathRemapper) -> None:
        self.store = store
        self.remapper = remapper

    def load(self, info_path: Path | str, tier: str, run_id: int | None = None) -> int:
        """Load an ``.info`` file into the store under *tier*.

        Returns the number of source files loaded.

        Optional run id credited for every DA line with a nonzero count.
        """
        info_path = Path(info_path)
        logger.info("Loading %s as %s coverage", info_path.name, tier)
        self.store.register_tier(tier)

        current_file: FileRecord | None = None
        files_loaded = 0

        with info_path.open() as f:
            for raw_line in f:
                line = raw_line.strip()

                if line.startswith("SF:"):
                    raw_path = line[3:]
                    resolved = self.remapper.resolve(raw_path)
                    if resolved is None:
                        logger.warning("Unmapped path, using raw: %s", raw_path)
                        resolved = Path(raw_path)
                    current_file = self.store.get_or_create_file(resolved)

                elif line.startswith("FNDA:") and current_file is not None:
                    hit = parse_fnda_record(line[5:])
                    fn = current_file.functions.get(hit.name)
                    if fn is None:
                        logger.warning(
                            "FNDA for %s in %s has no FN record; keeping its count at line 0",
                            hit.name,
                            current_file.path,
                        )
                        fn = current_file.get_or_create_function(hit.name)
                    fn.hits.add(tier, hit.count)

                elif line.startswith("FN:") and current_file is not None:
                    parsed = parse_fn_record(line[3:])
                    if parsed.name not in current_file.functions:
                        current_file.functions[parsed.name] = parsed

                elif line.startswith("DA:") and current_file is not None:
                    parts = line[3:].split(",")
                    lineno = int(parts[0])
                    count = int(parts[1])
                    lr = current_file.get_or_create_line(lineno)
                    lr.hits.add(tier, count)
                    if run_id is not None and count > 0:
                        lr.run_hits[run_id] = lr.run_hits.get(run_id, 0) + count

                elif line.startswith("BRDA:") and current_file is not None:
                    parts = line[5:].split(",")
                    block = _parse_block_id(parts[1])
                    if block is None:
                        continue  # exception-tagged: compiler machinery, not a product branch
                    lineno = int(parts[0])
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
