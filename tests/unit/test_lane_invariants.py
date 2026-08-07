"""Build-lane invariants: an addopts override must not drop the tach guard.

``-p no:tach`` in pyproject's ``addopts`` is the load-bearing guard against
tach's pytest plugin panicking otto's own harness (issue #193: its Rust
extension installs a C-level Ctrl-C handler at import, and consecutive
in-process pytest sessions panic ``MultipleHandlers``). Any lane that clears
``addopts`` with ``-o addopts=...`` silently drops that guard — the root
conftest's stub-module mitigation seeds too late (the ``pytest11`` entry
point has already loaded by conftest import), and the dev venv can carry
tach after a ``uv run --group lint`` (see pyproject's dependency-groups
note). Three lanes had done exactly that when this pin landed:
``tests_unit_repeat`` and the ``docs`` doctest leg in noxfile.py, and
``doctest-src`` in the Makefile (review 2026-08-06 §5.4, gate G13).

Scope: exactly the two build files, ``noxfile.py`` and ``Makefile``. Two
further ``--override-ini addopts=`` sites live in PRODUCT code
(``src/otto/suite/run.py``, ``src/otto/config/repo.py``) and re-create the
same exposure for otto's own in-process pytest sessions — a recorded live
defect (todo/churn-review-cheap-items-followups.md, "otto test panics when
tach is installed"). They are deliberately outside this pin, and travel with
the Wave 1 suite work (todo/test-infra-remediation-plan-2026-08-06.md).

The scanner is delimiter-aware line parsing, not "up to the next quote": the
value ends at the partner of the quote that actually delimits it — the quote
immediately BEFORE ``addopts`` (nox shape, ``"addopts=..."``) or immediately
AFTER the ``=`` (Make shape, ``-o addopts="..."``); unquoted values end at
whitespace, and an override whose quote never closes is reported as an
offender so unparseable is loud, never green. The first cut assumed the next
``"`` terminated the value, which meant a same-line comment *mentioning*
``-p no:tach`` — the most plausible way a human annotates removing it —
greened the guard on the comment text (caught in review; pinned below).
"""

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent

_QUOTES = ('"', "'")


def addopts_overrides(text: str) -> list[str]:
    """Every ``addopts=`` override value in *text*, comment lines excluded."""
    values: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for m in re.finditer(r"addopts=", line):
            start = m.end()
            before = line[m.start() - 1] if m.start() > 0 else ""
            after = line[start] if start < len(line) else ""
            if before in _QUOTES:
                quote, vstart = before, start
            elif after in _QUOTES:
                quote, vstart = after, start + 1
            else:
                # Unquoted: the value is the leading non-space run (possibly
                # empty). A spaced unquoted value truncates and reads as an
                # offender — fail-loud is the right direction for this guard.
                match = re.match(r"\S*", line[start:])
                values.append((match.group(0) if match else "").rstrip(","))
                continue
            end = line.find(quote, vstart)
            if end == -1:
                values.append(f"<unterminated addopts override: {line.strip()}>")
                continue
            values.append(line[vstart:end])
    return values


def test_addopts_overrides_keep_the_tach_guard() -> None:
    offenders = [
        f"{name}: addopts={value!r}"
        for name in ("noxfile.py", "Makefile")
        for value in addopts_overrides((_REPO / name).read_text())
        if "-p no:tach" not in value
    ]
    assert not offenders, (
        "addopts override(s) drop the issue-#193 tach guard — every "
        "`-o addopts=...` must re-state `-p no:tach` (the conftest stub seeds "
        "too late to protect plugin load):\n  " + "\n  ".join(offenders)
    )


def test_scanner_flags_a_guardless_override() -> None:
    """Positive control: the scanner observed red on both build-file shapes."""
    nox_bad = '        "addopts=",\n'
    nox_good = '        "addopts=-p no:tach",\n'
    make_bad = '\t@uv run pytest -o addopts="--doctest-modules" src/otto\n'
    make_good = '\t@uv run pytest -o addopts="--doctest-modules -p no:tach" src/otto\n'
    comment = "# clearing addopts= here would be bad\n"
    assert addopts_overrides(nox_bad) == [""]
    assert addopts_overrides(make_bad) == ["--doctest-modules"]
    assert all("-p no:tach" in v for v in addopts_overrides(nox_good + make_good))
    assert addopts_overrides(comment) == []


def test_scanner_is_not_fooled_by_a_same_line_mention() -> None:
    """Review catch: an annotated removal must stay red.

    Deleting the flag and leaving a comment naming it is the most plausible
    human edit; the comment text must never be read as the override's value.
    """
    annotated_nox = '        "addopts=",  # -p no:tach is unnecessary here\n'
    annotated_make = "\t@uv run pytest -o addopts='' src  # -p no:tach lives in pyproject\n"
    assert addopts_overrides(annotated_nox) == [""]
    assert addopts_overrides(annotated_make) == [""]


def test_scanner_sees_every_override_and_fails_loud_on_the_unparseable() -> None:
    two_on_one_line = '    session.run("pytest", "-o", "addopts=-p no:tach", "-o", "addopts=")\n'
    assert addopts_overrides(two_on_one_line) == ["-p no:tach", ""]
    bare_unquoted = "\t@uv run pytest -o addopts= src/otto\n"
    assert addopts_overrides(bare_unquoted) == [""]
    unterminated = '\t@uv run pytest -o addopts="--doctest-modules src/otto\n'
    (value,) = addopts_overrides(unterminated)
    assert "-p no:tach" not in value  # unparseable reads as an offender, never green
