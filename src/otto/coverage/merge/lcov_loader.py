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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..store.model import (
    BranchHits,
    CoverageStore,
    FileRecord,
    FunctionRecord,
)

if TYPE_CHECKING:
    from ..sources import SourceResolver

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


def _add_taken(have: "int | None", more: "int | None") -> "int | None":
    """Combine two ``BRDA`` taken counts: reached in either record is reached, counts add."""
    if have is None:
        return more
    if more is None:
        return have
    return have + more


@dataclass
class SourceRecord:
    """Everything one ``.info`` says about one source path, its records added up.

    A path can appear under more than one ``SF:`` in one file (an
    ``lcov -a`` concatenation, a per-object ``geninfo`` run); the counts add,
    as lcov's own merge adds them. Insertion order is the file's.
    """

    path: str
    lines: dict[int, int] = field(default_factory=dict)
    """``DA``: line number to hit count, summed across records."""
    branches: dict[tuple[int, int, int], int | None] = field(default_factory=dict)
    """``BRDA``: ``(line, block, branch)`` to taken count, ``None`` for never reached in
    any record; exception-tagged branches are dropped (``_parse_block_id``)."""
    functions: dict[str, FunctionRecord] = field(default_factory=dict)
    """``FN``: name to its record; the first record of a name wins."""
    function_hits: dict[str, int] = field(default_factory=dict)
    """``FNDA``: name to hit count, summed across records."""


def read_info(info_path: Path) -> dict[str, SourceRecord]:
    """Read an ``.info`` file into one :class:`SourceRecord` per source path.

    The one reader of lcov's text: the capture
    (:func:`otto.coverage.capture.model.parse_info`) and the store loader
    (:class:`LCOVLoader`) both consume these records, so a rule about the
    format — what a repeated path means, which branches count — lives here
    once.
    """
    records: dict[str, SourceRecord] = {}
    current: SourceRecord | None = None
    with info_path.open() as f:
        for raw_line in f:
            line = raw_line.strip()
            if line.startswith("SF:"):
                path = line[3:]
                current = records.get(path)
                if current is None:
                    current = records[path] = SourceRecord(path)
            elif current is None:
                continue
            elif line.startswith("FNDA:"):
                hit = parse_fnda_record(line[5:])
                current.function_hits[hit.name] = current.function_hits.get(hit.name, 0) + hit.count
            elif line.startswith("FN:"):
                parsed = parse_fn_record(line[3:])
                current.functions.setdefault(parsed.name, parsed)
            elif line.startswith("DA:"):
                parts = line[3:].split(",")
                lineno = int(parts[0])
                current.lines[lineno] = current.lines.get(lineno, 0) + int(parts[1])
            elif line.startswith("BRDA:"):
                lineno_s, block_s, branch_s, taken = line[5:].split(",")
                block = _parse_block_id(block_s)
                if block is None:
                    continue  # exception-tagged: compiler machinery, not a product branch
                key = (int(lineno_s), block, int(branch_s))
                count = None if taken == "-" else int(taken)
                current.branches[key] = _add_taken(current.branches.get(key), count)
            elif line == "end_of_record":
                current = None
    return records


class LCOVLoader:
    """Parse ``.info`` files into a :class:`~otto.coverage.store.model.CoverageStore` under a tier.

    Tier names are free-form strings.  The loader registers each tier
    with the store on first use so the store's ``tier_order`` reflects
    the data without callers having to pre-declare every tier.

    Each ``SF:`` path goes through *resolver* to its project file; a path it
    cannot place keeps its raw value, and the resolver alone logs it.
    With no resolver every path is kept as written.

    Example::

        store = CoverageStore(tier_order=["unit", "manual"])
        loader = LCOVLoader(store, SourceResolver(project_root))

        loader.load("unit_tests.info", "unit")
        loader.load("manual.info", "manual")
    """

    def __init__(self, store: CoverageStore, resolver: "SourceResolver | None" = None) -> None:
        self.store = store
        self.resolver = resolver

    def load(self, info_path: Path | str, tier: str, run_id: int | None = None) -> int:
        """Load an ``.info`` file into the store under *tier*.

        Returns the number of source files loaded.

        Optional run id credited for every DA line with a nonzero count.
        """
        info_path = Path(info_path)
        logger.info("Loading %s as %s coverage", info_path.name, tier)
        self.store.register_tier(tier)

        records = read_info(info_path)
        for raw_path, record in records.items():
            resolved = self.resolver.resolve(raw_path) if self.resolver else None
            current_file: FileRecord = self.store.get_or_create_file(resolved or Path(raw_path))

            for name, parsed in record.functions.items():
                if name not in current_file.functions:
                    current_file.functions[name] = parsed
            for name, count in record.function_hits.items():
                fn = current_file.functions.get(name)
                if fn is None:
                    logger.warning(
                        "FNDA for %s in %s has no FN record; keeping its count at line 0",
                        name,
                        current_file.path,
                    )
                    fn = current_file.get_or_create_function(name)
                fn.hits.add(tier, count)

            for lineno, count in record.lines.items():
                lr = current_file.get_or_create_line(lineno)
                lr.hits.add(tier, count)
                if run_id is not None and count > 0:
                    lr.run_hits[run_id] = lr.run_hits.get(run_id, 0) + count

            for (lineno, block, branch), taken in record.branches.items():
                reachable = taken is not None
                count = taken or 0
                lr = current_file.get_or_create_line(lineno)
                existing_map = {(b.block, b.branch): b for b in lr.branches}
                key = (block, branch)
                if key not in existing_map:
                    bh = BranchHits(block=block, branch=branch)
                    lr.branches.append(bh)
                    existing_map[key] = bh
                bh = existing_map[key]
                bh.set_reachable(tier, reachable)
                if reachable and count > 0:
                    bh.hits.add(tier, count)

        logger.info("Loaded %d files from %s", len(records), info_path.name)
        return len(records)
