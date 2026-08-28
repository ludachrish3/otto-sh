#!/usr/bin/env python3
"""Ask busybox.net whether it still serves the bytes ``busybox_pins.json`` was taken from.

Upstream publishes no signatures, so every pin in ``busybox_pins.json`` is
trust-on-first-use: bytes fetched at a reviewed moment, hashed, and committed.
Nothing stops upstream rebuilding an artifact in place afterwards, and this is
the one thing in the repo that would notice.

It is MONITORING, not a gate. Drift cannot change a byte this repo tests or
ships -- every consuming lane fetches the ``ci-assets-busybox-1`` mirror first
and verifies whatever answered against these same pins, so the pins, not the
host, decide what runs. A mismatch here is worth an issue and a look, never
worth blocking a push. It runs from nightly's ``busybox-upstream-drift`` job;
``tests/unit/host/test_busybox_artifacts.py`` pins that placement from three
sides.

The sweep itself is :func:`tests._fixtures.busybox.upstream_drift_report`,
which documents why it asks about EVERY pin rather than probing one the way
``preflight`` does. This module is the command-line half: the source-policy
refusal, the report, and the exit codes.

Usage, from the repo root::

    OTTO_BUSYBOX_SOURCE=upstream python scripts/check_busybox_upstream_drift.py

Exit codes: 0 upstream matches the pins, or upstream could not be reached; 1 at
least one artifact drifted or was withdrawn; 2 the source policy is wrong or
unreadable, so nothing was measured.

UNREACHABLE IS DELIBERATELY NOT A FAILURE, and it is the ONLY excused cause. A
third party being down says nothing about any byte, and failing on it files an
issue a night for as long as the outage lasts -- issue #267, a three-day
busybox.net outage, which is how a monitoring signal turns into noise nobody
reads. It is reported as a warning instead, so a run that checked nothing still
cannot be mistaken for a run that checked and found nothing.

WHAT LANDS IN THAT EXCUSED BUCKET IS THE WHOLE RISK. A 404 is not silence: the
host answered, and said the pinned file is gone. Excused as an outage it would
have stopped the sweep and exited 0 on ``BUSYBOX_MATRIX[0]`` -- 1.16.1, the
oldest artifact published anywhere and the likeliest to be pruned -- checking
ZERO pins, green, every night. So a deterministic refusal raises
``BusyBoxWithdrawnError`` and fails here instead.

Two is distinct from both because a policy error means the question was never
asked at all. It must never be 1: an uncaught policy error used to exit 1,
which this contract and the issue body the workflow files both read as
"upstream republished a pinned artifact" -- a workflow typo opening that ticket.
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests._fixtures.busybox import (  # noqa: E402
    BUSYBOX_MATRIX,
    DRIFT_MISMATCH,
    DRIFT_UNREACHABLE,
    DRIFT_VERIFIED,
    DRIFT_WITHDRAWN,
    BusyBoxUnavailableError,
    reads_upstream_only,
    upstream_drift_report,
)


def annotate(level: str, message: str) -> str:
    """Render *message* as a GitHub Actions annotation on a runner, a plain label elsewhere.

    The workflow wants ``::warning::`` so an unreachable upstream stays visible
    on a green run rather than buried in the log; a human running
    ``make busybox-drift`` wants a sentence. Same text either way, and the
    level is named in both -- the marker is presentation, not the verdict.
    """
    if os.environ.get("GITHUB_ACTIONS"):
        return f"::{level}::{message}"
    return f"{level.upper()}: {message}"


def main(argv: "list[str] | None" = None) -> int:
    """Sweep every pin against upstream and report drift, an outage, or a clean match."""
    parser = argparse.ArgumentParser(
        description="Verify busybox.net's current bytes against the committed pins.",
        epilog="Exit 0 matches or unreachable, 1 drift, 2 wrong source policy.",
    )
    parser.parse_args(argv)

    try:
        upstream_only = reads_upstream_only()
    except BusyBoxUnavailableError as e:
        # An unparseable policy is a POLICY error, and `_source_order` reports
        # it by raising. Uncaught, it left the interpreter with exit 1 — which
        # this script's own contract, and the issue body the workflow files,
        # both read as "upstream republished a pinned artifact". A typo in a
        # workflow env would have opened that ticket.
        print(f"drift check REFUSED: {e} Nothing was measured.", file=sys.stderr)
        return 2

    if not upstream_only:
        print(
            "drift check REFUSED: at least one fetch attempt would ask the "
            "ci-assets-busybox-1 mirror, which holds bytes taken FROM upstream and would "
            "answer 'still matches' about bytes upstream may no longer serve. Nothing was "
            "measured. Set OTTO_BUSYBOX_SOURCE=upstream (nightly's `busybox-upstream-drift` "
            "job does).",
            file=sys.stderr,
        )
        return 2

    with tempfile.TemporaryDirectory(prefix="busybox-drift-") as tmp:
        results = upstream_drift_report(Path(tmp))

    for result in results:
        print(f"  {result.release.filename}: {result.kind}")
        if result.detail:
            print(result.detail)

    drifted = [r.release for r in results if r.kind == DRIFT_MISMATCH]
    withdrawn = [r.release for r in results if r.kind == DRIFT_WITHDRAWN]
    verified = [r.release for r in results if r.kind == DRIFT_VERIFIED]
    unreachable = [r.release for r in results if r.kind == DRIFT_UNREACHABLE]

    if drifted:
        print(
            annotate(
                "error",
                f"busybox.net has REPUBLISHED {len(drifted)} pinned artifact(s) "
                f"({', '.join(r.filename for r in drifted)}). The pin is trust-on-first-use "
                f"and upstream publishes no signatures, so INVESTIGATE before anything "
                f"re-mirrors these bytes: confirm the banner still reports the pinned version "
                f"and the applet set is unchanged, then update busybox_pins.json in a "
                f"reviewed commit.",
            ),
            file=sys.stderr,
        )
        return 1

    if withdrawn:
        # Ranked below a rewrite and well above an outage. busybox.net
        # answered and refused, so nothing here is excusable as downtime.
        print(
            annotate(
                "error",
                f"busybox.net no longer publishes {len(withdrawn)} pinned artifact(s) "
                f"({', '.join(r.filename for r in withdrawn)}). Every source ANSWERED and "
                f"refused, so this is not an outage and no amount of waiting fixes it: the "
                f"pins name bytes that are gone from that address. The mirror still serves "
                f"them, so nothing this repo tests or ships is affected — but upstream "
                f"withdrawing a published binary is worth a look before the pin is repointed.",
            ),
            file=sys.stderr,
        )
        return 1

    if unreachable:
        print(
            annotate(
                "warning",
                f"busybox.net could not be reached, so {len(BUSYBOX_MATRIX) - len(verified)} "
                f"of {len(BUSYBOX_MATRIX)} pins went unchecked this run. Upstream being down "
                f"says nothing about any byte — every consuming lane fetches the mirror and "
                f"verifies against the same pins — so this is not a failure. It IS a gap in "
                f"monitoring: if it persists for days, drift is going undetected.",
            )
        )
        return 0

    print(f"busybox.net still serves all {len(verified)} pinned artifacts unchanged.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
