"""Script a git repo through time and assert per-line coverage dispositions.

The heart of spec §10: each aging-repo scenario is `commit → capture →
mutate → fold → assert`. Folding goes straight through
``apply_manual_capture`` (the unit under test); the full reporter
pipeline is exercised elsewhere (test_capture_report_cycle).
"""

from datetime import datetime
from pathlib import Path

from otto.coverage.capture import gitio
from otto.coverage.capture.model import Capture, CaptureFileCov
from otto.coverage.store.model import CoverageStore
from otto.coverage.validity import apply_manual_capture, register_capture_run
from tests._fixtures.gitrepo import TmpGitRepo


class RepoTimeline(TmpGitRepo):
    """A TmpGitRepo with pinned dates plus the capture→fold→assert verbs of
    spec §10 — dates pinned because timeline scenarios pass ``today=``
    explicitly and want reproducible SHAs."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, dates="2026-01-01T00:00:00Z")
        self.captures: list[Capture] = []

    def commit(self, msg: str = "c", *, allow_empty: bool = True) -> str:
        # Timeline scenarios script empty "time passes" commits on purpose —
        # the pre-rebase RepoTimeline always allowed them.
        return super().commit(msg, allow_empty=allow_empty)

    def capture(
        self,
        label: str,
        lines: dict[str, dict[int, int]],
        *,
        tier: str = "manual",
        host: str = "bench-1",
        captured_at: str = "2026-07-01T00:00:00Z",
        ticket: str | None = None,
    ) -> Capture:
        files = {
            rel: CaptureFileCov(blob=gitio.blob_sha(self.root, Path(rel)), lines=dict(hits))
            for rel, hits in lines.items()
        }
        cap = Capture(
            tier=tier,
            base_commit=gitio.head_commit(self.root),
            captured_at=captured_at,
            board=host,
            display_name=label,
            ticket=ticket,
            files=files,
        )
        self.captures.append(cap)
        return cap

    def fold(
        self, *, max_age_days: int | None = None, today: datetime | None = None
    ) -> CoverageStore:
        store = CoverageStore()
        for cap in self.captures:
            run_id = register_capture_run(store, cap)
            apply_manual_capture(
                store,
                cap,
                self.root,
                max_age_days=max_age_days,
                today=today,
                run_id=run_id,
            )
        return store

    def dispositions(self, store: CoverageStore, rel: str) -> dict[int, str]:
        rec = store.get_or_create_file(self.root / rel)
        out: dict[int, str] = {}
        for lineno, lr in sorted(rec.lines.items()):
            if lr.hits.is_hit():
                out[lineno] = "hit"
            elif lr.state in ("stale", "aging"):
                out[lineno] = lr.state
            else:
                out[lineno] = "none"
        return out
