"""Script a git repo through time and assert per-line coverage dispositions.

The heart of spec §10: each aging-repo scenario is `commit → capture →
mutate → fold → assert`. Folding goes straight through
``apply_manual_capture`` (the unit under test); the full reporter
pipeline is exercised elsewhere (test_capture_report_cycle).
"""

import subprocess
from datetime import datetime
from pathlib import Path

from otto.coverage.capture import gitio
from otto.coverage.capture.model import Capture, CaptureFileCov
from otto.coverage.store.model import CoverageStore
from otto.coverage.validity import apply_manual_capture, register_capture_run

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    "PATH": "/usr/bin:/bin",
}


class RepoTimeline:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.captures: list[Capture] = []
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
            env={**_GIT_ENV, "HOME": str(self.root)},
        ).stdout

    def write(self, rel: str, text: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self, msg: str = "c") -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", msg, "--allow-empty")
        return self.git("rev-parse", "HEAD").strip()

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
