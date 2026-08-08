"""The gate is only as good as its adoption.

`AGENTS.md` is the one instruction surface that exists INSIDE a worktree: a
worktree checkout contains exactly the tracked files, and `.claude` is
gitignored in full, so hook- or settings-based reminders are absent precisely
where the agent is working. This pins that the instruction cannot vanish in an
unrelated edit.
"""

from tests._fixtures.paths import PROJECT_ROOT

AGENTS_MD = PROJECT_ROOT / "AGENTS.md"


def test_agents_md_tells_agents_to_run_the_fresh_gate():
    text = AGENTS_MD.read_text()
    assert "make gate-fresh" in text, (
        "AGENTS.md must name the gate — it is the only instruction surface "
        "present inside a worktree (see docs/superpowers/specs/2026-08-07-gate-fresh-design.md)"
    )


def test_agents_md_says_when_to_run_it():
    # A bare mention would let the bullet decay into a name with no trigger.
    text = AGENTS_MD.read_text().lower()
    assert "before any squash" in text or "hand back" in text
